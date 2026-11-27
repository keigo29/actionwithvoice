import json
import time
import unittest
import urllib.error
import urllib.request

from ray import server
from ray.app import Assistant
from ray.ui import StateHub, serve, session_value
from tests.test_ray import FakeBrain, cfg

TOKEN = "t" * 20


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class RemoteSpeakerTest(unittest.TestCase):
    def test_sentence_carries_its_scene_and_stop_cuts(self):
        hub = StateHub()
        sp = server.RemoteSpeaker(hub)
        sp.say("一つ目。", on_start=lambda: hub.set(scene={"kind": "todos"}))
        self.assertEqual(hub.state["utter"], {"id": 1, "text": "一つ目。", "scene": {"kind": "todos"}})
        sp.say("", on_start=lambda: hub.set(scene={"kind": "habits"}))  # 声のない場面：画面だけ変える
        self.assertEqual(hub.state["utter"]["id"], 1)
        sp.stop()
        self.assertEqual(hub.state["cut"], 1)
        self.assertTrue(sp.wait())

    def test_answer_goes_to_phone_not_speaker(self):
        hub = StateHub()
        a = Assistant(cfg(), FakeBrain(["@画面 todos\n今日のやることです。"]), server.RemoteSpeaker(hub), hub,
                      log=lambda *x, **k: None)
        server.RemoteRay(a)._run("やることは？")
        self.assertEqual(hub.state["utter"]["text"], "今日のやることです。")
        self.assertEqual(hub.state["utter"]["scene"]["kind"], "todos")
        self.assertEqual(hub.state["state"], "idle")
        self.assertFalse(a.busy.is_set())


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.asked, self.stopped = [], []
        self.srv = serve(StateHub(), 0, remote={"token": TOKEN, "ask": self.asked.append, "stop": lambda: self.stopped.append(1)})
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.cookie = f"ray_session={session_value(TOKEN)}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, path, data=None, headers=None, cookie=True):
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = self.cookie
        r = urllib.request.Request(self.base + path, data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with urllib.request.build_opener(NoRedirect).open(r, timeout=3) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_page_needs_login(self):
        code, h, _ = self.req("/", cookie=False)
        self.assertEqual((code, h["Location"]), (303, "/login"))
        for path in ("/events", "/vendor/diagram-draw.js", "/shot/a.png"):
            self.assertEqual(self.req(path, cookie=False)[0], 401, path)
        code, _, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"window.RAY_REMOTE = true", body)

    def test_login(self):
        t0 = time.monotonic()
        code, _, body = self.req("/login", data=b"token=wrong", cookie=False)
        self.assertEqual(code, 401)
        self.assertIn("合言葉が違います", body.decode())
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)  # 総当たりを遅くする
        code, h, _ = self.req("/login", data=f"token={TOKEN}".encode(), cookie=False)
        self.assertEqual(code, 303)
        self.assertIn(session_value(TOKEN), h["Set-Cookie"])
        self.assertNotIn(TOKEN, h["Set-Cookie"])  # 合言葉そのものは cookie に入れない
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Strict", h["Set-Cookie"])

    def test_ask_and_stop(self):
        body = json.dumps({"text": "今日の予定は？"}).encode()
        self.assertEqual(self.req("/api/ask", data=body, headers={"X-Ray": "1"})[0], 202)
        self.assertEqual(self.asked, ["今日の予定は？"])
        self.assertEqual(self.req("/api/ask", data=body)[0], 401)                              # 決まったヘッダーが無い
        self.assertEqual(self.req("/api/ask", data=body, headers={"X-Ray": "1"}, cookie=False)[0], 401)  # ログインしていない
        self.assertEqual(self.req("/api/ask", data=b'{"text": " "}', headers={"X-Ray": "1"})[0], 400)
        self.assertEqual(self.req("/api/stop", data=b"{}", headers={"X-Ray": "1"})[0], 202)
        self.assertEqual(self.stopped, [1])
        self.assertEqual(len(self.asked), 1)

    def test_desk_ray_is_unchanged(self):
        srv = serve(StateHub(), 0)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/", timeout=3) as r:
                body = r.read()
            self.assertNotIn(b"window.RAY_REMOTE = true", body)
        finally:
            srv.shutdown()
            srv.server_close()


class StartTest(unittest.TestCase):
    def test_refuses_short_token(self):
        c = cfg()
        c["server"]["token"] = "short"
        with self.assertRaises(SystemExit):
            server.run(c)


if __name__ == "__main__":
    unittest.main()
