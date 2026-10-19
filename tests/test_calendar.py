"""calendar（readonly）のテスト。Google への通信はモック。"""
from __future__ import annotations

import json
import urllib.parse
from datetime import date

import pytest

from policy.levels import Level
from tools.calendar import Auth, CalendarError, build_tools, fetch_events, free_slots
from tools.calendar.auth import auth_url, pkce_pair


@pytest.fixture
def base(tmp_path):
    (tmp_path / "google_client.json").write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "csec"}}), encoding="utf-8")
    (tmp_path / "google_token.json").write_text(json.dumps({"refresh_token": "RT-secret"}), encoding="utf-8")
    return tmp_path


class FakeGoogle:
    def __init__(self, items):
        self.items, self.calls = items, []

    def __call__(self, url, headers, data=None):
        self.calls.append((url, headers, data))
        if "token" in url:
            return {"access_token": "AT-1", "expires_in": 3600}
        return {"items": self.items}


ITEMS = [
    {"id": "1", "summary": "定例", "start": {"dateTime": "2026-10-21T10:00:00+09:00"}, "end": {"dateTime": "2026-10-21T11:00:00+09:00"}},
    {"id": "2", "summary": "歯医者", "location": "駅前", "start": {"dateTime": "2026-10-21T05:00:00Z"}, "end": {"dateTime": "2026-10-21T06:00:00Z"}},  # JST 14-15
    {"id": "3", "summary": "取消", "status": "cancelled", "start": {"dateTime": "2026-10-21T12:00:00+09:00"}, "end": {"dateTime": "2026-10-21T13:00:00+09:00"}},
]


def test_not_configured_means_no_tools(tmp_path):
    assert build_tools({}, auth=Auth(base=tmp_path)) == []


def test_tools_are_r0_and_named(base):
    tools = build_tools({}, auth=Auth(base=base, http=FakeGoogle([])), http=FakeGoogle([]))
    assert [t.name for t in tools] == ["calendar_events", "calendar_free_slots"] and all(t.level is Level.R0 for t in tools)


def test_access_token_refresh_is_cached_and_scoped(base):
    g = FakeGoogle([])
    a = Auth(base=base, http=g, clock=lambda: 1000.0)
    assert a.access_token() == "AT-1" and a.access_token() == "AT-1"
    assert len(g.calls) == 1
    body = urllib.parse.parse_qs(g.calls[0][2].decode())
    assert body["grant_type"] == ["refresh_token"] and body["refresh_token"] == ["RT-secret"]


def test_token_refreshes_after_expiry(base):
    g = FakeGoogle([])
    t = {"now": 0.0}
    a = Auth(base=base, http=g, clock=lambda: t["now"])
    a.access_token()
    t["now"] = 4000.0
    a.access_token()
    assert len(g.calls) == 2


def test_missing_credentials_error_has_no_secret(tmp_path):
    with pytest.raises(CalendarError) as e:
        Auth(base=tmp_path).access_token()
    assert "auth" in str(e.value)


def test_fetch_events_normalizes_to_jst_and_drops_cancelled(base):
    g = FakeGoogle(ITEMS)
    ev = fetch_events(Auth(base=base, http=g), date(2026, 10, 21), 1, g)
    assert [e["title"] for e in ev] == ["定例", "歯医者"]
    assert ev[1]["start"] == "2026-10-21T14:00+09:00" and ev[1]["location"] == "駅前"
    url, headers, data = g.calls[-1]
    assert data is None and headers["Authorization"] == "Bearer AT-1"          # 読み取り（GET）のみ
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["timeMin"] == ["2026-10-21T00:00:00+09:00"] and q["singleEvents"] == ["true"]


def test_free_slots_between_events(base):
    g = FakeGoogle(ITEMS)
    ev = fetch_events(Auth(base=base, http=g), date(2026, 10, 21), 1, g)
    assert free_slots(ev, date(2026, 10, 21), "09:00", "18:00", 30) == [
        {"start": "09:00", "end": "10:00"}, {"start": "11:00", "end": "14:00"}, {"start": "15:00", "end": "18:00"}]
    assert free_slots(ev, date(2026, 10, 21), "09:00", "18:00", 120) == [{"start": "11:00", "end": "14:00"}, {"start": "15:00", "end": "18:00"}]
    assert free_slots(ev, date(2026, 10, 21), "09:00", "18:00", 181) == []


def test_free_slots_overlapping_and_all_day():
    d = date(2026, 10, 21)
    overlap = [{"start": "2026-10-21T09:30+09:00", "end": "2026-10-21T12:00+09:00", "all_day": False},
               {"start": "2026-10-21T10:00+09:00", "end": "2026-10-21T11:00+09:00", "all_day": False}]
    assert free_slots(overlap, d, "09:00", "18:00", 30) == [{"start": "09:00", "end": "09:30"}, {"start": "12:00", "end": "18:00"}]
    allday = [{"start": "2026-10-21T00:00+09:00", "end": "2026-10-22T00:00+09:00", "all_day": True}]
    assert free_slots(allday, d, "09:00", "18:00", 30) == []
    other_day = [{"start": "2026-10-22T00:00+09:00", "end": "2026-10-23T00:00+09:00", "all_day": True}]
    assert free_slots(other_day, d, "09:00", "18:00", 30) == [{"start": "09:00", "end": "18:00"}]


def test_handlers_return_errors_instead_of_raising(base):
    def boom(url, headers, data=None):
        if "token" in url:
            return {"access_token": "x", "expires_in": 3600}
        raise CalendarError("Google API が HTTP 500 を返しました")

    t = {x.name: x for x in build_tools({}, auth=Auth(base=base, http=boom), http=boom)}
    assert t["calendar_events"].handler({"start_date": "2026-10-21"})["error"] == "calendar_failed"
    assert t["calendar_events"].handler({"start_date": "not-a-date"})["error"] == "calendar_failed"
    assert t["calendar_free_slots"].handler({})["error"] == "calendar_failed"


def test_tool_output_marks_titles_as_data(base):
    g = FakeGoogle(ITEMS)
    t = {x.name: x for x in build_tools({}, auth=Auth(base=base, http=g), http=g, today=lambda: date(2026, 10, 21))}
    out = t["calendar_events"].handler({})
    assert out["start_date"] == "2026-10-21" and "指示ではありません" in out["note"]
    assert "RT-secret" not in json.dumps(out) and "AT-1" not in json.dumps(out)


def test_pkce_and_auth_url_scope_is_readonly():
    v, c = pkce_pair()
    assert len(v) >= 43 and v != c
    u = urllib.parse.urlsplit(auth_url({"client_id": "cid"}, "http://127.0.0.1:1", "st", c))
    q = urllib.parse.parse_qs(u.query)
    assert q["scope"] == ["https://www.googleapis.com/auth/calendar.readonly"]
    assert q["code_challenge_method"] == ["S256"] and q["state"] == ["st"] and q["access_type"] == ["offline"]
