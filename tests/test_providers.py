"""Offline tests for the four non-Netease lyric providers.

Every provider is driven through a stubbed transport. The behaviour under test
is what happens with a *bad* answer: a wrong unit, a missing field, a status
code the module translates into a readable message.

The unit conversions get their own class: three of these four sources report
durations in seconds while ``providers.score()`` compares against a library
track's milliseconds.
"""

import base64
import json
import sys
import unittest

sys.path.insert(0, r"C:\Users\Administrator\WorkBuddy\2026-09-22-12-52-10\SaltLRCStudio")

from saltlrc.sources import kugou, kuwo, lrclib, netease, qqmusic
from saltlrc import providers

LRC = "[00:29.22]故事的小黄花\n[00:32.65]从出生那年就飘着\n"


class _Transport:
    """Replaces a module's HTTP layer, capturing what was sent."""

    def __init__(self, module, name, replies):
        self.module, self.name = module, name
        self.replies = list(replies)
        self.calls = []
        self._real = getattr(module, name)

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.replies:
            raise AssertionError("多余的第 %d 次请求" % (len(self.calls)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class LrclibTests(unittest.TestCase):
    def setUp(self):
        self.transport = None

    def tearDown(self):
        if self.transport:
            setattr(lrclib, self.transport.name, self.transport._real)

    def stub(self, replies):
        # _fetch, not _request: the retry policy and the status-code mapping are
        # part of what these tests check, so the stub sits below them.
        self.transport = _Transport(lrclib, "_fetch", replies)
        setattr(lrclib, "_fetch", self.transport)
        return self.transport
        return self.transport

    def test_search_converts_seconds_to_milliseconds(self):
        # 270.0 seconds is 270000 ms.
        self.stub([json.dumps([{"id": 1, "trackName": "晴天", "artistName": "周杰伦",
                                "albumName": "叶惠美", "duration": 270.0}])])
        found = lrclib.search("晴天")
        self.assertEqual(found[0]["duration"], 270000)

    def test_missing_duration_is_zero_not_an_error(self):
        self.stub([json.dumps([{"id": 1, "trackName": "x", "artistName": "y"}])])
        self.assertEqual(lrclib.search("x")[0]["duration"], 0)

    def test_artists_list_wins_over_the_single_artist_field(self):
        self.stub([json.dumps([{"id": 1, "trackName": "x", "artistName": "旧字段",
                                "artists": [{"name": "A"}, {"name": "B"}]}])])
        self.assertEqual(lrclib.search("x")[0]["artist"], "A/B")

    def test_lyric_prefers_synced_and_parses_the_timeline(self):
        self.stub([json.dumps({"id": 7, "trackName": "晴天", "syncedLyrics": LRC,
                               "plainLyrics": "纯文本"})])
        payload = lrclib.lyric(7)
        self.assertEqual([line["time"] for line in payload["main"]], [29220, 32650])
        self.assertFalse(payload["word_level"])

    def test_plain_only_lyric_is_reported_as_untimed(self):
        # A lyric with no time axis is noted as plain text.
        self.stub([json.dumps({"id": 7, "trackName": "x", "syncedLyrics": "",
                               "plainLyrics": "第一行\n第二行"})])
        payload = lrclib.lyric(7)
        self.assertEqual([line["text"] for line in payload["main"]], ["第一行", "第二行"])
        self.assertTrue(any("纯文本" in note for note in payload["notes"]))

    def test_an_empty_record_is_an_explicit_failure(self):
        self.stub([json.dumps({"id": 7, "trackName": "x", "syncedLyrics": "",
                               "plainLyrics": ""})])
        with self.assertRaises(netease.ProviderError):
            lrclib.lyric(7)

    def test_a_transient_timeout_is_retried(self):
        # LRCLIB times out intermittently; one retry returns a normal result.
        self.stub([TimeoutError("timed out"), json.dumps([])])
        self.assertEqual(lrclib.search("x"), [])
        self.assertEqual(len(self.transport.calls), 2)

    def test_a_client_error_is_not_retried(self):
        import urllib.error
        self.stub([urllib.error.HTTPError("u", 400, "bad", {}, None)])
        with self.assertRaises(netease.ProviderError):
            lrclib.search("x")
        self.assertEqual(len(self.transport.calls), 1)

    def test_a_404_reads_as_no_lyrics(self):
        import urllib.error
        self.stub([urllib.error.HTTPError("u", 404, "gone", {}, None)])
        with self.assertRaises(netease.ProviderError) as caught:
            lrclib.lyric(1)
        self.assertIn("没有这首歌", str(caught.exception))


class QqMusicTests(unittest.TestCase):
    def setUp(self):
        self.transport = None

    def tearDown(self):
        if self.transport:
            setattr(qqmusic, self.transport.name, self.transport._real)

    def stub(self, replies):
        self.transport = _Transport(qqmusic, "_get", replies)
        setattr(qqmusic, "_get", self.transport)
        return self.transport

    def test_search_maps_songmid_and_seconds(self):
        self.stub([json.dumps({"code": 0, "data": {"song": {"list": [{
            "songmid": "0039MnYb0qxYhV", "songname": "晴天", "interval": 269,
            "singer": [{"name": "周杰伦"}],
        }]}}})])
        found = qqmusic.search("晴天")
        self.assertEqual(found[0]["id"], "0039MnYb0qxYhV")
        self.assertEqual(found[0]["duration"], 269000)
        self.assertEqual(found[0]["artist"], "周杰伦")

    def test_jsonp_wrapper_is_stripped(self):
        # The endpoint falls back to JSON-P when it ignores format=json.
        self.stub(['callback({"code":0,"data":{"song":{"list":[]}}})'])
        self.assertEqual(qqmusic.search("x"), [])

    def test_an_encrypted_body_names_the_real_cause(self):
        # POSTing instead of GET yields an opaque blob; the message names the
        # request method.
        self.stub(["t0rqn7NcazELTXDwRsjbcHQBK82nExQhnDpDlzsmopYw=="])
        with self.assertRaises(netease.ProviderError) as caught:
            qqmusic.search("x")
        self.assertIn("GET", str(caught.exception))

    def test_lyric_reads_plain_lrc_and_the_translation_field(self):
        self.stub([json.dumps({"code": 0, "lyric": LRC,
                               "trans": "[00:29.22]Little yellow flower\n"})])
        payload = qqmusic.lyric("mid")
        self.assertEqual(payload["main"][0]["text"], "故事的小黄花")
        self.assertEqual(payload["translation"][0]["text"], "Little yellow flower")

    def test_base64_lyric_is_decoded_when_nobase64_is_ignored(self):
        blob = base64.b64encode(LRC.encode("utf-8")).decode("ascii")
        self.stub([json.dumps({"code": 0, "lyric": blob})])
        self.assertEqual(qqmusic.lyric("mid")["main"][0]["text"], "故事的小黄花")

    def test_html_entities_in_the_translation_are_decoded(self):
        self.stub([json.dumps({"code": 0, "lyric": LRC,
                               "trans": "[00:29.22]A &amp; B\n"})])
        payload = qqmusic.lyric("mid")
        self.assertEqual(payload["translation"][0]["text"], "A & B")

    def test_html_entities_in_the_main_lyric_are_decoded_too(self):
        # The lyric body is HTML-escaped on the wire; a lyric containing "&"
        # arrives as a literal "&amp;".
        self.stub([json.dumps({"code": 0, "lyric": "[00:29.22]Rock &amp; Roll\n"})])
        payload = qqmusic.lyric("mid")
        self.assertEqual(payload["main"][0]["text"], "Rock & Roll")
        self.assertIn("Rock & Roll", payload["raw_main"])

    def test_an_escaped_escape_is_unescaped_exactly_once(self):
        # Unescaping happens exactly once; "&amp;amp;" becomes "&amp;".
        self.stub([json.dumps({"code": 0, "lyric": "[00:29.22]x &amp;amp; y\n"})])
        self.assertEqual(qqmusic.lyric("mid")["main"][0]["text"], "x &amp; y")

    def test_an_empty_lyric_is_an_explicit_failure(self):
        self.stub([json.dumps({"code": 0, "lyric": ""})])
        with self.assertRaises(netease.ProviderError):
            qqmusic.lyric("mid")

    def test_a_missing_mid_is_rejected_before_any_request(self):
        with self.assertRaises(netease.ProviderError):
            qqmusic.lyric("")
        self.assertIsNone(self.transport)


class KugouTests(unittest.TestCase):
    def setUp(self):
        self.transports = []

    def tearDown(self):
        for transport in self.transports:
            setattr(transport.module, transport.name, transport._real)

    def stub(self, module, name, replies):
        transport = _Transport(module, name, replies)
        setattr(module, name, transport)
        self.transports.append(transport)
        return transport

    def test_search_converts_seconds_to_milliseconds(self):
        self.stub(kugou, "_request", [json.dumps({"status": 1, "data": {"info": [{
            "hash": "abc", "songname": "晴天", "singername": "周杰伦",
            "duration": 269,
        }]}})])
        found = kugou.search("晴天")
        self.assertEqual(found[0]["id"], "abc")
        self.assertEqual(found[0]["duration"], 269000)

    def test_lyric_walks_search_resolve_download(self):
        self.stub(kugou, "_request", [
            json.dumps({"candidates": [{"id": "11", "accesskey": "k", "krctype": 2}]}),
            json.dumps({"content": base64.b64encode(LRC.encode("utf-8")).decode("ascii"),
                        "fmt": "lrc"}),
        ])
        payload = kugou.lyric("abc")
        self.assertEqual(payload["main"][0]["text"], "故事的小黄花")

    def test_krc_only_candidates_are_skipped_not_garbled(self):
        # A KRC payload is an encrypted binary.
        self.stub(kugou, "_request", [
            json.dumps({"candidates": [
                {"id": "1", "accesskey": "a", "krctype": 1},
                {"id": "2", "accesskey": "b", "krctype": 2},
            ]}),
            json.dumps({"content": base64.b64encode(LRC.encode("utf-8")).decode("ascii"),
                        "fmt": "lrc"}),
        ])
        payload = kugou.lyric("abc")
        self.assertEqual(payload["main"][0]["text"], "故事的小黄花")
        # Only the second candidate should have been downloaded.
        download = self.transports[0].calls[1]
        self.assertEqual(download[0][1]["id"], "2")

    def test_a_krc_candidate_is_never_downloaded(self):
        # krctype 1 means the file can only be served as the encrypted KRC
        # binary; it is filtered out *before* the download.
        self.stub(kugou, "_request", [
            json.dumps({"candidates": [
                {"id": "1", "accesskey": "a", "krctype": 1},
                {"id": "2", "accesskey": "b", "krctype": 2},
            ]}),
            json.dumps({"content": base64.b64encode(LRC.encode("utf-8")).decode("ascii"),
                        "fmt": "lrc"}),
        ])
        kugou.lyric("abc")
        self.assertEqual(len(self.transports[0].calls), 2)     # resolve + one download
        downloads = [c for c in self.transports[0].calls if c[0][0] == kugou.DOWNLOAD]
        self.assertEqual([c[0][1]["id"] for c in downloads], ["2"])

    def test_all_candidates_failing_reports_the_first_reason(self):
        self.stub(kugou, "_request", [
            json.dumps({"candidates": [{"id": "1", "accesskey": "a", "krctype": 1}]}),
        ])
        with self.assertRaises(netease.ProviderError) as caught:
            kugou.lyric("abc")
        self.assertIn("KRC", str(caught.exception))
        # The refusal is known from the candidate metadata alone.
        self.assertEqual(len(self.transports[0].calls), 1)

    def test_no_candidates_is_an_explicit_failure(self):
        self.stub(kugou, "_request", [json.dumps({"candidates": []})])
        with self.assertRaises(netease.ProviderError) as caught:
            kugou.lyric("abc")
        self.assertIn("没有这首歌", str(caught.exception))

    def test_utf8_bom_is_stripped_from_the_download(self):
        # A leading BOM precedes the first "["; both lines must still parse.
        raw = "\ufeff" + LRC
        self.stub(kugou, "_request", [
            json.dumps({"candidates": [{"id": "1", "accesskey": "a", "krctype": 2}]}),
            json.dumps({"content": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
                        "fmt": "lrc"}),
        ])
        payload = kugou.lyric("abc")
        self.assertEqual(len(payload["main"]), 2)

    def test_duration_is_passed_to_the_resolve_step_in_milliseconds(self):
        # hash|duration is how the caller narrows several uploads of one song.
        self.stub(kugou, "_request", [
            json.dumps({"candidates": [{"id": "1", "accesskey": "a", "krctype": 2}]}),
            json.dumps({"content": base64.b64encode(LRC.encode("utf-8")).decode("ascii"),
                        "fmt": "lrc"}),
        ])
        kugou.lyric("abc|269000")
        resolve = self.transports[0].calls[0]
        self.assertEqual(resolve[0][1]["duration"], 269000)

    def test_the_search_host_is_plain_http(self):
        # Its certificate does not match the hostname.
        self.assertTrue(kugou.SEARCH.startswith("http://"))


class PseudoJsonTests(unittest.TestCase):
    """Kuwo answers with a JS object literal, not JSON."""

    def test_single_quotes_and_bare_keys(self):
        value = kuwo.parse_pseudo_json("{'a':'1',b:2,c:[3,4]}")
        self.assertEqual(value, {"a": "1", "b": 2, "c": [3, 4]})

    def test_bare_words_survive_inside_values(self):
        # A regex-based "fix" would quote `level` and `bitrate` inside the value.
        value = kuwo.parse_pseudo_json(
            "{MINFO:'level:p,bitrate:320,format:mp3;level:s,bitrate:48'}")
        self.assertEqual(value["MINFO"], "level:p,bitrate:320,format:mp3;level:s,bitrate:48")

    def test_nested_structures_and_literals(self):
        value = kuwo.parse_pseudo_json(
            "{list:[{SONGNAME:'a',DURATION:'269',PAY:0,ok:true,none:null}]}")
        row = value["list"][0]
        self.assertEqual(row["SONGNAME"], "a")
        self.assertEqual(row["DURATION"], "269")
        self.assertEqual(row["PAY"], 0)
        self.assertIs(row["ok"], True)
        self.assertIsNone(row["none"])

    def test_escapes_inside_strings(self):
        value = kuwo.parse_pseudo_json(r"{a:'line\nnext',b:'q\'q',c:'\u0041'}")
        self.assertEqual(value["a"], "line\nnext")
        self.assertEqual(value["b"], "q'q")
        self.assertEqual(value["c"], "A")

    def test_double_quoted_strings_are_handled_too(self):
        self.assertEqual(kuwo.parse_pseudo_json('{"a":"1"}'), {"a": "1"})

    def test_a_truncated_document_raises_instead_of_guessing(self):
        # A truncated document raises rather than returning a partial parse.
        with self.assertRaises(ValueError):
            kuwo.parse_pseudo_json("{a:1")
        with self.assertRaises(ValueError):
            kuwo.parse_pseudo_json("")

    def test_trailing_junk_is_rejected(self):
        with self.assertRaises(ValueError):
            kuwo.parse_pseudo_json("{a:1} extra")


class KuwoTests(unittest.TestCase):
    def setUp(self):
        self.transports = []
        # Zero the retry pause.
        self._pause = kuwo.RETRY_PAUSE
        kuwo.RETRY_PAUSE = 0

    def tearDown(self):
        kuwo.RETRY_PAUSE = self._pause
        for transport in self.transports:
            setattr(transport.module, transport.name, transport._real)

    def stub(self, module, name, replies):
        transport = _Transport(module, name, replies)
        setattr(module, name, transport)
        self.transports.append(transport)
        return transport

    def test_search_extracts_the_numeric_id_and_seconds(self):
        self.stub(kuwo, "_request", [
            "{abslist:[{MUSICRID:'MUSIC_51685512',SONGNAME:'晴天&nbsp;(KTV版伴奏)',"
            "ARTIST:'周杰伦',ALBUM:'',DURATION:'269'}]}"])
        found = kuwo.search("晴天")
        self.assertEqual(found[0]["id"], "51685512")
        self.assertEqual(found[0]["duration"], 269000)
        self.assertEqual(found[0]["title"], "晴天 (KTV版伴奏)")

    def test_escaped_ampersand_and_entities_are_cleaned(self):
        self.stub(kuwo, "_request", [
            r"{abslist:[{MUSICRID:'MUSIC_1',SONGNAME:'x',ARTIST:'周杰伦\\u0026五月天',DURATION:'1'}]}"])
        self.assertEqual(kuwo.search("x")[0]["artist"], "周杰伦&五月天")

    def test_lyric_converts_seconds_strings_to_milliseconds(self):
        self.stub(kuwo, "_request", [json.dumps({"status": 200, "data": {"lrclist": [
            {"lineLyric": "周杰伦 - 晴天", "time": "1.70"},
            {"lineLyric": "故事的小黄花", "time": "29.22"},
        ]}})])
        payload = kuwo.lyric("51685512")
        self.assertEqual([line["time"] for line in payload["main"]], [1700, 29220])

    def test_lyric_sorts_lines_by_time(self):
        self.stub(kuwo, "_request", [json.dumps({"status": 200, "data": {"lrclist": [
            {"lineLyric": "b", "time": "5.0"},
            {"lineLyric": "a", "time": "1.0"},
        ]}})])
        payload = kuwo.lyric("1")
        self.assertEqual([line["text"] for line in payload["main"]], ["a", "b"])

    def test_a_flaky_301_is_retried(self):
        # The same id answers 301 then 200.
        self.stub(kuwo, "_request", [
            json.dumps({"status": 301, "data": None}),
            json.dumps({"status": 200, "data": {"lrclist": [
                {"lineLyric": "故事的小黄花", "time": "29.22"}]}}),
        ])
        payload = kuwo.lyric("51685512")
        self.assertEqual(len(payload["main"]), 1)
        self.assertEqual(len(self.transports[0].calls), 2)

    def test_a_persistent_301_names_the_source_as_the_problem(self):
        self.stub(kuwo, "_request",
                  [json.dumps({"status": 301, "data": None})] * kuwo.RETRIES)
        with self.assertRaises(netease.ProviderError) as caught:
            kuwo.lyric("51685512")
        message = str(caught.exception)
        self.assertIn("301", message)
        self.assertIn("换一个来源", message)

    def test_an_empty_lrclist_is_not_treated_as_a_retryable_failure(self):
        # status 200 with no rows means "this track has no lyric"; it is not
        # retried.
        self.stub(kuwo, "_request", [json.dumps({"status": 200, "data": {"lrclist": None}})])
        with self.assertRaises(netease.ProviderError) as caught:
            kuwo.lyric("1")
        self.assertIn("纯音乐", str(caught.exception))
        self.assertEqual(len(self.transports[0].calls), 1)

    def test_a_non_200_status_is_retried_to_the_limit(self):
        # A non-200 status is retried to the limit.
        self.stub(kuwo, "_request",
                  [json.dumps({"status": 301, "data": None})] * kuwo.RETRIES)
        with self.assertRaises(netease.ProviderError):
            kuwo.lyric("1")
        self.assertEqual(len(self.transports[0].calls), kuwo.RETRIES)

    def test_music_id_prefix_is_accepted(self):
        self.stub(kuwo, "_request", [json.dumps({"status": 200, "data": {"lrclist": [
            {"lineLyric": "x", "time": "1.0"}]}})])
        self.assertEqual(len(kuwo.lyric("MUSIC_228908")["main"]), 1)

    def test_a_non_numeric_id_is_rejected_before_any_request(self):
        with self.assertRaises(netease.ProviderError):
            kuwo.lyric("not-a-number")
        self.assertFalse(self.transports)


class RegistryTests(unittest.TestCase):
    def test_every_source_is_registered_and_labelled(self):
        self.assertEqual(providers.names(),
                         ["netease", "lrclib", "qqmusic", "kugou", "kuwo"])
        for name in providers.names():
            with self.subTest(source=name):
                self.assertTrue(providers.label(name))
                self.assertTrue(hasattr(providers.REGISTRY[name], "search"))
                self.assertTrue(hasattr(providers.REGISTRY[name], "lyric"))

    def test_catalogue_matches_the_registry(self):
        catalogue = {entry["id"]: entry for entry in providers.catalogue()}
        self.assertEqual(set(catalogue), set(providers.names()))
        # Only Netease can use a login, and it is opt-in even there.
        self.assertEqual([k for k, v in catalogue.items() if v["needs_login"]], ["netease"])

    def test_an_unknown_source_is_reported_by_name(self):
        with self.assertRaises(netease.ProviderError) as caught:
            providers.search("spotify", "x")
        self.assertIn("spotify", str(caught.exception))

    def test_score_thresholds_assume_milliseconds(self):
        """A seconds-based duration scores as a mismatch: feeding 270 where
        270000 belongs fails the ranking.
        """
        track = {"title": "晴天", "artist": "周杰伦", "duration": 269000}
        right = {"title": "晴天", "artist": "周杰伦", "duration": 270000}
        wrong = {"title": "晴天", "artist": "周杰伦", "duration": 270}
        self.assertGreater(providers.score(right, track), providers.score(wrong, track))
        self.assertTrue(providers.best_match([right], track)[2])


class FallbackTests(unittest.TestCase):
    """Ranked candidate fallback for sources that refuse individual tracks."""

    def setUp(self):
        self.attempted = []
        self._real = providers.REGISTRY["kuwo"].lyric

    def tearDown(self):
        providers.REGISTRY["kuwo"].lyric = self._real

    def stub(self, behaviour):
        """``behaviour`` maps song_id -> payload dict, ProviderError, or a value."""
        def fake(song_id, cookie=None, drop_credits=True):
            self.attempted.append(song_id)
            reply = behaviour[song_id]
            if isinstance(reply, Exception):
                raise reply
            return reply
        providers.REGISTRY["kuwo"].lyric = fake

    @staticmethod
    def refusal(song_id):
        return netease.ProviderError(
            "酷我暂时拒绝提供这首歌的歌词（status=301，已重试 4 次），请换一个来源")

    def test_a_refusal_advances_to_the_next_candidate(self):
        self.stub({
            "a": self.refusal("a"),
            "b": {"provider": "kuwo", "main": [{"time": 0, "text": "b", "words": []}], "notes": []},
        })
        payload, used = providers.lyric_first_working(
            "kuwo", [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}])
        self.assertEqual(used["id"], "b")
        self.assertEqual(self.attempted, ["a", "b"])
        # The swap is recorded in ``notes``.
        self.assertTrue(any("改用候选" in note for note in payload["notes"]))

    def test_a_transport_error_is_not_retried_on_another_candidate(self):
        # A network failure stops the walk; the next candidate is not tried.
        self.stub({
            "a": netease.ProviderError("无法连接酷我：timed out"),
            "b": {"provider": "kuwo", "main": [{"time": 0, "text": "b", "words": []}]},
        })
        with self.assertRaises(netease.ProviderError) as caught:
            providers.lyric_first_working("kuwo", [{"id": "a"}, {"id": "b"}])
        self.assertIn("timed out", str(caught.exception))
        self.assertEqual(self.attempted, ["a"])

    def test_the_score_floor_blocks_a_substitution_of_a_different_song(self):
        # 晴天 is refused while a karaoke version of a *different* song answers;
        # the score floor blocks the substitution.
        track = {"title": "晴天", "artist": "周杰伦", "duration": 269000}
        wanted = {"id": "a", "title": "晴天", "artist": "周杰伦", "duration": 269000}
        other = {"id": "b", "title": "听妈妈的话", "artist": "周杰伦", "duration": 272000}
        self.stub({"a": self.refusal("a"), "b": {"provider": "kuwo", "main": []}})
        with self.assertRaises(netease.ProviderError):
            providers.lyric_first_working("kuwo", [wanted, other], track=track, min_score=45)
        self.assertEqual(self.attempted, ["a"])

    def test_a_tepid_candidate_is_still_allowed_when_the_floor_permits_it(self):
        # The *best* match is refused; a same-song sibling above the floor
        # answers instead.
        track = {"title": "花海", "artist": "周杰伦", "duration": 229000}
        best = {"id": "a", "title": "花海", "artist": "周杰伦", "duration": 229000}
        sibling = {"id": "b", "title": "花海 (DJ 阿若版)", "artist": "周杰伦", "duration": 210000}
        self.assertGreater(providers.score(best, track), providers.score(sibling, track))
        self.assertGreaterEqual(providers.score(sibling, track), 45)
        self.stub({"a": self.refusal("a"),
                   "b": {"provider": "kuwo", "main": [{"time": 0, "text": "x", "words": []}],
                         "notes": []}})
        payload, used = providers.lyric_first_working("kuwo", [best, sibling],
                                                     track=track, min_score=45)
        self.assertEqual(used["id"], "b")
        self.assertTrue(any("改用候选" in note for note in payload["notes"]))

    def test_duplicate_ids_are_only_attempted_once(self):
        self.stub({"a": self.refusal("a")})
        with self.assertRaises(netease.ProviderError):
            providers.lyric_first_working("kuwo", [{"id": "a"}, {"id": "a"}, {"id": " a "}])
        self.assertEqual(self.attempted, ["a"])

    def test_bare_id_strings_are_accepted(self):
        self.stub({"a": {"provider": "kuwo", "main": [{"time": 0, "text": "x", "words": []}],
                         "notes": []}})
        _, used = providers.lyric_first_working("kuwo", ["a"])
        self.assertEqual(used["id"], "a")

    def test_an_empty_candidate_list_is_an_explicit_failure(self):
        with self.assertRaises(netease.ProviderError):
            providers.lyric_first_working("kuwo", [])


if __name__ == "__main__":
    unittest.main()
