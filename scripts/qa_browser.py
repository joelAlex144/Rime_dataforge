#!/usr/bin/env python3
"""The browser gate: Playwright (chromium, headless) against the real client.

    set -a; source .env; set +a          # ENRICH_PROVIDER for the upload's navigator
    python scripts/qa_browser.py --runs 3
    # step 6 needs the navigator inside 120 s: a hosted model, e.g.
    #   ENRICH_PROVIDER=groq GROQ_MODEL=openai/gpt-oss-20b python scripts/qa_browser.py --runs 3

It starts `TTS_PROVIDER=fake python examples/policy-reader/server.py --dev`
(--port, default 8080; use another when your own server holds 8080) and
`npm run dev` (port 5173) itself, restarts the server for every run so each
run starts from a fresh session, and asserts every step on the visible text
of <main>, printing each with a timestamp. On a WSL machine whose node is the
Windows one, a Vite dev server runs on the Windows side and a WSL browser
cannot reach it: the gate then builds the bundle (`npm run build`) and opens
the server's own URL, which serves the same client from web/dist -- it says
which it used.

  1. open http://localhost:5173; no "Unreviewed document" banner
  2. click Play; within 8 s of reading starting the clause area holds policy
     text and none of "Fixture document", "Provenance", "Word count",
     "Source URL"; at least 5 topic chips are visible. The first Play opens
     the welcome / the invitation (audio-paced, 320 ms a word with the fake
     voice); the gate answers those through the ask box the way a listener
     would ("arogya sanjeevani", "from the start", "from the top") and counts
     the 8 s from the last answer.
  3. click Pause; the same clause text is still in the clause area; the
     status says paused
  4. click Play; within 6 s the reading advanced (the clause changed or the
     heard part grew); still no provenance strings
  5. type "what does that mean" + Enter; within 20 s an answer appears; the
     trace has answer_source; reading resumes
  6. upload a stamped copy of fixture_saral_jeevan_bima.docx via "Add a
     document" while reading (the identical file is, by design, a duplicate
     of the committed fixture and would not be ingested); within 15 s a
     companion line appears; the trace has no cue_skipped reason=reading;
     within 120 s the "topic in mind" invitation appears

Exit 0 only if every step of every run passed.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "examples" / "policy-reader" / "web"
DOCX = ROOT / "examples" / "policy-reader" / "fixtures" / "source" / "fixture_saral_jeevan_bima.docx"
FORBIDDEN = ("Fixture document", "Provenance", "Word count", "Source URL")
ASK = 'input[aria-label="Ask about what you just heard"]'


def http_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_http(url: str, seconds: float) -> bool:
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def stamped_copy(tmp: Path) -> Path:
    import docx
    d = docx.Document(str(DOCX))
    d.add_paragraph(f"QA browser gate copy, made {time.strftime('%Y-%m-%d %H:%M:%S')}.")
    out = tmp / f"fixture_saral_jeevan_bima_qa_{int(time.time())}.docx"
    d.save(str(out))
    return out


class Run:
    def __init__(self, n: int, page, base: str, client: str = "http://localhost:5173") -> None:
        self.n, self.page, self.base, self.client = n, page, base, client
        self.t0 = time.monotonic()
        self.failures: list = []

    def t(self) -> str:
        return f"{time.monotonic() - self.t0:6.1f}s"

    def say(self, step: str, line: str) -> None:
        print(f"[run {self.n}][{self.t()}] {step}: {line}", flush=True)

    def fail(self, step: str, line: str) -> None:
        self.failures.append(f"{step}: {line}")
        self.say(step, "FAIL " + line)

    # ---- the page
    def main_text(self) -> str:
        return self.page.locator("main").inner_text()

    def doc_text(self) -> str:
        t = self.page.locator("main .doc").inner_text().strip()
        return "" if t == "Nothing is being read yet." else t

    def heard_len(self) -> int:
        try:
            return len(self.page.locator("main .doc .heard").inner_text())
        except Exception:
            return 0

    def prompt_text(self) -> str:
        loc = self.page.locator('main [aria-label="Prompt"]')
        return loc.inner_text().strip() if loc.count() else ""

    def reply(self, text: str) -> None:
        self.page.fill(ASK, text)
        self.page.press(ASK, "Enter")

    def events(self, kind: str = None) -> list:
        # /api/events returns a bounded tail; ask by type so early records are
        # not scrolled out by audio bookkeeping.
        url = f"{self.base}/api/events" + (f"?type={kind}" if kind else "")
        recs = http_json(url).get("records", [])
        return [r for r in recs if kind is None or r.get("type") == kind]

    def answer_prompts(self, answered: set) -> bool:
        """The first Play opens the welcome or the invitation: answer as a
        listener would, once per prompt. Returns True if a reply was sent."""
        pt = self.prompt_text()
        if not pt:
            return False
        for marker, reply in (("Which one, or upload a new one", "arogya sanjeevani"),
                              ("topic you have in mind", "from the start"),
                              ("Where shall we start", "from the top")):
            if marker in pt and (marker, pt[:40]) not in answered:
                answered.add((marker, pt[:40]))
                self.reply(reply)
                self.say("2", f"prompt heard ({marker}) -> replied {reply!r}")
                return True
        return False

    def warm(self) -> None:
        """The server warms Docling and the answer model at start, in threads
        that starve the loop for a while; a run starts once both are done."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120:
            types = {t for t in ("docling_warm", "docling_unavailable", "llm_ready", "llm_unavailable") if self.events(t)}
            if ("docling_warm" in types or "docling_unavailable" in types) and ("llm_ready" in types or "llm_unavailable" in types):
                self.say("0", f"server warm after {time.monotonic() - t0:.1f} s ({'docling_warm' in types and 'docling' or 'no docling'}, "
                              f"{'llm_ready' in types and 'answer model ready' or 'answer model unavailable'})")
                return
            time.sleep(0.5)
        self.say("0", "server not warm after 120 s; running anyway")

    def clean_leftovers(self) -> None:
        for e in http_json(f"{self.base}/api/library").get("documents", []):
            if (e.get("title") or "").startswith("fixture_saral_jeevan_bima_qa"):
                req = urllib.request.Request(f"{self.base}/documents/{e['doc_id']}", method="DELETE")
                try:
                    urllib.request.urlopen(req, timeout=10).read()
                    self.say("0", f"removed a leftover upload {e['name']}")
                except Exception as ex:
                    self.say("0", f"could not remove leftover {e['name']}: {ex}")

    # ---- the steps
    def run(self) -> bool:
        page = self.page
        self.warm()
        self.clean_leftovers()
        # 1
        page.goto(self.client, wait_until="domcontentloaded")
        page.wait_for_selector("main .rail-row, nav .rail-row", timeout=20000)
        page.wait_for_function("() => document.querySelectorAll('nav .rail-row').length >= 5", timeout=20000)
        time.sleep(1.0)
        main = self.main_text()
        if "Unreviewed document" in main:
            self.fail("1", "the Unreviewed banner shows")
        else:
            self.say("1", f"page open at {self.client}; no Unreviewed banner; documents in the rail: "
                     f"{page.locator('nav .rail-row').count() - 1}")
        # 2
        page.click('button[aria-label="Play"]')
        t_last = time.monotonic()
        answered: set = set()
        reading_at = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.doc_text():
                reading_at = time.monotonic()
                break
            if self.answer_prompts(answered):
                t_last = time.monotonic()
            time.sleep(0.25)
        if reading_at is None:
            self.fail("2", "no clause text within 90 s of Play")
            return False
        since = reading_at - t_last
        doc = self.doc_text()
        bad = [w for w in FORBIDDEN if w in doc]
        chips = page.locator('main [aria-label="Topics"] button').count()
        ok = since <= 8.0 and not bad and chips >= 5
        (self.say if ok else self.fail)("2", f"clause text {since:.1f} s after the last click/reply "
                                          f"({'no' if not bad else bad} provenance strings); chips={chips}; "
                                          f"clause: {doc[:80]!r}")
        # 3
        before = self.doc_text()
        page.click('button[aria-label="Pause"]')
        t_pause = time.monotonic()
        paused = False
        while time.monotonic() - t_pause < 5:
            if "Paused here" in self.main_text():
                paused = True
                break
            time.sleep(0.1)
        after = self.doc_text()
        same = after == before
        (self.say if (paused and same) else self.fail)("3", f"paused={paused}; same clause on screen={same}; "
                                                        f"{after[:60]!r}")
        heard_at_pause = self.heard_len()
        # 4
        page.click('button[aria-label="Play"]')
        t_play = time.monotonic()
        advanced = None
        while time.monotonic() - t_play < 6:
            d = self.doc_text()
            if d and (d != after or self.heard_len() > heard_at_pause):
                advanced = time.monotonic() - t_play
                break
            time.sleep(0.1)
        d = self.doc_text()
        bad = [w for w in FORBIDDEN if w in d]
        ok = advanced is not None and not bad
        (self.say if ok else self.fail)("4", f"advanced after {advanced and round(advanced, 1)} s "
                                          f"({'clause changed' if d != after else 'heard part grew'}); "
                                          f"provenance strings: {bad or 'none'}")
        # 5
        self.reply("what does that mean")
        t_ask = time.monotonic()
        answer = ""
        while time.monotonic() - t_ask < 20:
            loc = page.locator('main section[aria-label="Answer"]')
            if loc.count() and loc.inner_text().strip():
                answer = loc.inner_text().strip()
                break
            time.sleep(0.2)
        has_source = bool(self.events("answer_source")) or any("answer_source" in r for r in self.events("answer_grounded"))
        resumed = False
        t_r = time.monotonic()
        while time.monotonic() - t_r < 25:
            if page.locator('button[aria-label="Pause"]').count() or "Reading section" in self.main_text():
                resumed = True
                break
            time.sleep(0.2)
        ok = bool(answer) and has_source and resumed
        (self.say if ok else self.fail)("5", f"answer after {time.monotonic() - t_ask:.1f} s: {answer[:70]!r}; "
                                          f"answer_source in trace={has_source}; reading resumed={resumed}")
        # 6
        tmp = Path(os.environ.get("QA_TMP", "/tmp"))
        copy = stamped_copy(tmp)
        page.click('nav button.rail-row:has-text("Add a document")')
        page.set_input_files('input[aria-label="Document file"]', str(copy))
        t_up = time.monotonic()
        line = ""
        while time.monotonic() - t_up < 15:
            loc = page.locator('main [aria-label="Companion line"]')
            if loc.count() and loc.inner_text().strip():
                line = loc.inner_text().strip()
                break
            time.sleep(0.2)
        skipped = [r for r in self.events("cue_skipped") if r.get("reason") == "reading"]
        (self.say if (line and not skipped) else self.fail)(
            "6", f"companion line after {time.monotonic() - t_up:.1f} s: {line[:80]!r}; cue_skipped reason=reading: {len(skipped)}")
        invited = None
        while time.monotonic() - t_up < 120:
            if "topic you have in mind" in self.main_text():
                invited = time.monotonic() - t_up
                break
            time.sleep(0.5)
        (self.say if invited is not None else self.fail)(
            "6", f"invitation after {invited and round(invited, 1)} s" if invited is not None
            else f"no invitation within 120 s (enrichment: started={len(self.events('enrich_started'))} "
                 f"done={len(self.events('enrich_done'))} failed={len(self.events('enrich_failed'))})")
        gaps = [g for g in self.events("narration_gap_ms")]
        if gaps:
            self.say("6", f"narration_gap_ms max={gaps[-1].get('max')} count={gaps[-1].get('count')}")
        # the run's upload is not left in the library
        for e in http_json(f"{self.base}/api/library").get("documents", []):
            if "qa" in e["name"].lower() or (e.get("title") or "").startswith("fixture_saral_jeevan_bima_qa"):
                req = urllib.request.Request(f"{self.base}/documents/{e['doc_id']}", method="DELETE")
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        self.say("6", f"deleted the run's upload {e['name']}: HTTP {r.status}")
                except Exception as ex:
                    self.say("6", f"delete of {e['name']} failed: {ex}")
        return not self.failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright
    base = f"http://127.0.0.1:{args.port}"
    logs = Path(os.environ.get("QA_TMP", "/tmp"))
    vite = None
    client = "http://localhost:5173"
    if wait_http("http://localhost:5173/", 2):
        print("vite dev server already on 5173: using it (it serves the working tree live)", flush=True)
    else:
        vite = subprocess.Popen(["npm", "run", "dev", "--", "--port", "5173", "--strictPort"], cwd=str(WEB),
                                stdout=open(logs / "qa_browser_vite.log", "w"), stderr=subprocess.STDOUT)
        if not wait_http("http://localhost:5173/", 20):
            # A Windows-side Vite (Windows node under WSL) is not reachable from
            # a WSL browser: serve the built bundle from the reader itself.
            vite.terminate()
            vite = None
            print("vite on 5173 not reachable from here: building the bundle and using the server's own URL", flush=True)
            subprocess.run(["npm", "run", "build"], cwd=str(WEB), check=True,
                           stdout=open(logs / "qa_browser_build.log", "w"), stderr=subprocess.STDOUT)
            client = f"http://127.0.0.1:{args.port}"
    results = []
    try:
        for n in range(1, args.runs + 1):
            if wait_http(f"{base}/api/status", 1):
                print(f"something already answers on port {args.port}; stop it or pass another --port", file=sys.stderr)
                return 2
            env = dict(os.environ, TTS_PROVIDER="fake")
            srv = subprocess.Popen([sys.executable, "-u", str(ROOT / "examples" / "policy-reader" / "server.py"),
                                    "--dev", "--port", str(args.port)], env=env, cwd=str(ROOT),
                                   stdout=open(logs / f"qa_browser_server_{n}.log", "w"), stderr=subprocess.STDOUT)
            try:
                if not wait_http(f"{base}/api/status", 60):
                    print("server did not come up", file=sys.stderr)
                    return 2
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=not args.headed,
                                                args=["--autoplay-policy=no-user-gesture-required"])
                    page = browser.new_page()
                    page.set_default_timeout(20000)
                    run = Run(n, page, base, client)
                    try:
                        ok = run.run()
                    except Exception as e:
                        run.fail("x", f"exception: {e!r}")
                        ok = False
                    results.append(ok)
                    print(f"[run {n}] {'PASS' if ok else 'FAIL: ' + '; '.join(run.failures)}", flush=True)
                    browser.close()
            finally:
                srv.terminate()
                try:
                    srv.wait(timeout=10)
                except Exception:
                    srv.kill()
    finally:
        if vite is not None:
            vite.terminate()
    print(f"{sum(results)}/{len(results)} runs passed", flush=True)
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
