import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import update_results as u


def calendar_fixture():
    # Fixed synthetic baseline: tests must not change as the live ICS gains results.
    fixtures = [
        ("rpl-r8-spartak-rostov", "20260913T170000", "Спартак — Ростов (РПЛ)"),
        ("rpl-r7-dynamo-spartak", "20260906T183000", "Динамо 2:1 Спартак (РПЛ)"),
        ("cup-r2-rubin-spartak", "20260819T183000", "Рубин 1:1 Спартак (Кубок России) (4:3 пен.)"),
        ("cup-r6-orenburg-spartak", "20261126T170000", "Оренбург — Спартак (Кубок России)"),
    ]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0"]
    for source_id, start, summary in fixtures:
        lines.extend(["BEGIN:VEVENT", f"UID:{source_id}@test", "SEQUENCE:1",
                      f"DTSTART;TZID=Europe/Moscow:{start}",
                      f"SUMMARY:{summary}", "DESCRIPTION:Тестовый матч",
                      f"X-SOURCE-ID:championat-{source_id}", "STATUS:CONFIRMED", "END:VEVENT"])
    return "\n".join(lines + ["END:VCALENDAR", ""])


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = cls(2026, 9, 8, 12, tzinfo=u.MOSCOW)
        return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)


def sports_row(day="13.09.2026", time="17:00", opponent="Ростов", home=True,
               competition="rpl", score="превью"):
    tournament = "rfpl" if competition == "rpl" else "russian-cup"
    return f'''<tr><td>{day}{' | ' + time if time else ''}</td>
    <td><a href="https://www.sports.ru/football/tournament/{tournament}/">Турнир</a></td>
    <td><a href="https://www.sports.ru/football/club/opponent/">{opponent}</a>
    <td>{'Дома' if home else 'В гостях'}</td></td>
    <td class="score-td"><a>{score}</a></td></tr>'''


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(u, "datetime", FrozenDateTime)
        clock.start()
        self.addCleanup(clock.stop)
        self.original = calendar_fixture()
        self.existing = u.parse_existing_events(self.original)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        ics, state = Path(folder.name) / "calendar.ics", Path(folder.name) / "state.json"
        ics.write_text(self.original)
        state.write_text('{"version": 2, "events": {}}')
        for name, value in (("ICS_PATH", ics), ("STATE_PATH", state)):
            replacement = patch.object(u, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.quiet = redirect_stderr(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        u.DIAGNOSTICS.clear()

    def parse(self, **kwargs):
        return u.parse_sports_html(sports_row(**kwargs), self.existing)[0]

    def test_sports_schedule_keeps_source_id(self):
        event = self.parse()
        self.assertEqual(event["id"], "championat-rpl-r8-spartak-rostov")
        self.assertEqual(event["start"].hour, 17)
        self.assertIsNone(event["withheld"])

    def test_sports_away_score_is_home_away_not_spartak_first(self):
        event = self.parse(day="06.09.2026", time="18:30", opponent="Динамо", home=False, score="2 : 1")
        self.assertEqual((event["home_key"], event["score_home"], event["score_away"]), ("dynamo", 2, 1))

    def test_missing_time_is_withheld_not_midnight(self):
        self.assertIsNotNone(self.parse(time="")["withheld"])

    def test_cup_draw_without_penalties_is_withheld(self):
        event = self.parse(day="19.08.2026", time="18:30", opponent="Рубин", home=False,
                           competition="cup", score="1 : 1")
        self.assertIsNotNone(event["withheld"])

    def test_official_date_conflict_is_withheld(self):
        event = self.parse(day="24.11.2026", time="03:00", opponent="Оренбург", home=False, competition="cup")
        self.assertIn("расходятся", event["withheld"])

    def test_unknown_fixture_is_rejected(self):
        with self.assertRaises(u.SourceError):
            self.parse(opponent="Неизвестный клуб")

    def test_ambiguous_anchor_is_rejected(self):
        self.existing += self.existing
        with self.assertRaises(u.SourceError):
            self.parse()

    def test_wrong_season_is_not_used(self):
        self.assertEqual(u.parse_sports_html(sports_row(day="13.09.2025"), self.existing), [])

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(u.SourceError):
            self.parse(score="перенесён")

    def test_scheduled_score_in_future_is_rejected(self):
        with self.assertRaises(u.SourceError):
            self.parse(day="13.06.2027", score="1 : 0")

    def test_partial_source_is_rejected(self):
        with self.assertRaises(u.SourceError):
            u.validate_source_events([self.parse()], "rpl")

    def test_duplicate_round_is_rejected(self):
        events = [dict(self.parse(), id=str(i)) for i in range(30)]
        with self.assertRaises(u.SourceError):
            u.validate_source_events(events, "rpl")

    def test_championat_parser_legacy_markup(self):
        raw = '<tr><td>Тур 8 13.09.2026 17:00 Спартак М – Ростов - : -</td></tr>'
        event = u.parse_championat_html(raw, "https://source.test/", "rpl")[0]
        self.assertEqual(event["id"], "championat-rpl-r8-spartak-rostov")

    def test_content_failure_tries_next_mirror(self):
        with patch.object(u, "fetch_text", side_effect=[("no calendar", "a"), ("valid", "b")]) as fetch, \
             patch.object(u, "parse_championat_html", side_effect=[u.SourceError("bad content"), [self.parse()]]), \
             patch.object(u, "validate_source_events"):
            self.assertEqual(len(u.parse_championat_calendar(("a", "b"), "rpl")), 1)
            self.assertEqual(fetch.call_args_list[1].args, ("b",))

    def test_sberid_http_200_is_not_calendar_or_retried(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'<title>Authorization SberID</title>'
        response.headers.get_content_charset.return_value = "utf-8"
        response.headers.get_content_type.return_value = "text/html"
        response.status = 200
        response.geturl.return_value = "https://source.test/"
        with patch.object(u.urllib.request, "urlopen", return_value=response) as fetch:
            with self.assertRaises(u.SourceError):
                u.fetch_text("https://source.test/")
            self.assertEqual(fetch.call_count, 1)

    def test_network_errors_are_bounded_and_retry(self):
        with patch.object(u.urllib.request, "urlopen", side_effect=TimeoutError("timeout")) as fetch, \
             patch.object(u.time, "sleep"):
            with self.assertRaises(u.SourceError):
                u.fetch_text(("https://a.test/", "https://b.test/"))
            self.assertEqual(fetch.call_count, 4)

    def test_second_observation_required(self):
        event = self.parse()
        state, stable = u.update_state({"events": {}}, [event])
        self.assertFalse(stable)
        _, stable = u.update_state(state, [event])
        self.assertEqual(stable, {event["id"]})
        event["start"] += u.timedelta(minutes=30)
        _, stable = u.update_state(state, [event])
        self.assertFalse(stable)

    def test_uid_and_alarms_preserved_on_update(self):
        event = self.parse()
        updated, _ = u.apply_events(self.original, self.existing, [event], {event["id"]})
        u.validate_calendar(updated)
        self.assertEqual({e.uid for e in self.existing}, {e.uid for e in u.parse_existing_events(updated)})
        old = next(e for e in self.existing if e.source_id == event["id"])
        new = next(e for e in u.parse_existing_events(updated) if e.uid == old.uid)
        self.assertIn("TRIGGER:-PT1H", new.block)
        self.assertIn("TRIGGER:-PT5M", new.block)

    def test_all_sources_failure_leaves_calendar_and_state_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            ics, state = Path(folder) / "calendar.ics", Path(folder) / "state.json"
            ics.write_text(self.original)
            state.write_text('{"version": 2, "events": {}}')
            before = (ics.read_bytes(), state.read_bytes())
            with patch.object(u, "ICS_PATH", ics), patch.object(u, "STATE_PATH", state), \
                 patch.object(u, "collect_events", side_effect=u.SourceError("all failed")):
                with self.assertRaises(u.SourceError):
                    u.main()
            self.assertEqual(before, (ics.read_bytes(), state.read_bytes()))

    def test_dry_run_does_not_write_or_advance_observations(self):
        with patch.object(u, "collect_events", return_value=([self.parse()], [])), \
             patch.object(u, "parse_rfs_cup_results", return_value=set()), \
             patch.object(u, "atomic_write") as write, redirect_stdout(io.StringIO()):
            self.assertEqual(u.main(dry_run=True), 0)
            write.assert_not_called()

    def test_validation_failure_prevents_all_writes(self):
        with patch.object(u, "collect_events", return_value=([self.parse()], [])), \
             patch.object(u, "parse_rfs_cup_results", return_value=set()), \
             patch.object(u, "apply_events", return_value=("invalid", 1)), \
             patch.object(u, "atomic_write") as write:
            with self.assertRaises(ValueError):
                u.main()
            write.assert_not_called()

    def test_regression_from_result_to_schedule_prevents_writes(self):
        event = self.parse(day="06.09.2026", time="18:30", opponent="Динамо", home=False)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "calendar.ics"
            path.write_text(self.original)
            with patch.object(u, "ICS_PATH", path), \
                 patch.object(u, "collect_events", return_value=([event], [])), \
                 patch.object(u, "parse_rfs_cup_results", return_value=set()), \
                 patch.object(u, "atomic_write") as write:
                with self.assertRaises(u.SourceError):
                    u.main()
                write.assert_not_called()

    def test_withheld_event_does_not_advance_state(self):
        event = self.parse(time="")
        with patch.object(u, "collect_events", return_value=([event], [])), \
             patch.object(u, "parse_rfs_cup_results", return_value=set()), \
             patch.object(u, "update_state") as update:
            with self.assertRaises(u.SourceError):
                u.main(dry_run=True)
            update.assert_not_called()

    def test_interrupted_atomic_replace_keeps_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "calendar.ics"
            path.write_text(self.original)
            with patch.object(u.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    u.atomic_write(path, "new content")
            self.assertEqual(path.read_text(), self.original)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_atomic_write(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "calendar.ics"
            u.atomic_write(path, self.original)
            self.assertEqual(path.read_text(), self.original)
            self.assertEqual(list(Path(folder).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
