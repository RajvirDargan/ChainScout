"""LLM backends, behind one small interface.

Two implementations, because the Gemini API key turned out to be the binding
constraint: the API reports a hard free-tier cap of 20 requests/day for
gemini-3.8-flash, and even gemini-3.7-flash managed only 2 of 9 requests when asked
to carry load alone. The Antigravity CLI authenticates against the operator's own
plan instead, so it is preferred and the API is kept as a fallback.

Callers use `complete(prompt, schema)` and never learn which backend answered — except
through `last_backend` / `last_model`, which the digest footer reports so a fallback is
visible rather than silent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests

# macOS ARG_MAX is 1 MiB; stay well under it before falling back to stdin.
ARGV_PROMPT_LIMIT = 120_000


class BackendUnavailable(Exception):
    """Raised at construction when a backend cannot be used at all."""


def _extract_json(text: str):
    """Pull a JSON value out of a model response that may be fenced or prose-wrapped."""
    if not text:
        return None
    text = text.strip()

    # Try the text as-is FIRST. Stripping fences up front corrupted the agy envelope:
    # it is valid JSON whose `response` field *contains* a fenced string, so the fence
    # regex matched that inner content and destroyed the outer object. That is what made
    # batches fail intermittently - only when the model chose to fence its answer.
    try:
        return json.loads(text)
    except ValueError:
        pass

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except ValueError:
            text = fence.group(1).strip()
    # Last resort: the outermost {...} or [...] in the response.
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = text.find(opener), text.rfind(closer)
        if 0 <= i < j:
            try:
                return json.loads(text[i:j + 1])
            except ValueError:
                continue
    return None


class AntigravityCLI:
    """Headless `agy`. Uses the cached keyring session from a one-time interactive login."""

    name = "antigravity"

    def __init__(self, cfg: dict, quota=None, log=print):
        self.binary = cfg.get("binary", "agy")
        self.model = cfg.get("model") or ""
        self.timeout = int(cfg.get("timeout_seconds", 300))
        self.quota = quota
        self.log = log
        self.last_model = self.model or "account-default"
        self.calls = 0

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def preflight(self) -> tuple[bool, str]:
        """Confirm the binary exists AND is authenticated, before a run depends on it.

        In a non-TTY an unauthenticated agy exits non-zero rather than hanging, which is
        exactly what makes this checkable from launchd.
        """
        if not self.available():
            return False, f"{self.binary} not installed"
        try:
            r = subprocess.run([self.binary, "-p", "reply with: ok",
                                "--output-format", "json",
                                "--print-timeout", "60s"],
                               capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return False, "agy timed out on preflight"
        except OSError as exc:
            return False, f"agy could not start: {exc}"
        if r.returncode != 0:
            return False, f"agy exit {r.returncode}: {(r.stderr or '')[:160]}"
        return True, "ok"

    def _dump_envelope(self, stdout: str):
        """Keep an unparsable envelope for inspection instead of discarding it."""
        try:
            logs = Path(__file__).resolve().parent.parent / "logs"
            logs.mkdir(exist_ok=True)
            path = logs / f"agy-unparsable-{int(time.time())}.json"
            path.write_text(stdout or "")
            self.log(f"      raw envelope saved to {path.name}")
        except OSError:
            pass

    def complete(self, prompt: str, schema: dict | None = None) -> object | None:
        if self.quota is not None and not self.quota.check_and_reserve("antigravity", 1):
            return None

        # --print-timeout takes a Go duration ("300s"), not a bare number; passing an
        # integer makes agy exit 2 before it does any work.
        cmd = [self.binary, "--output-format", "json",
               "--print-timeout", f"{self.timeout}s"]
        if self.model:
            cmd += ["--model", self.model]

        schema_file = None
        try:
            if schema:
                schema_file = tempfile.NamedTemporaryFile(
                    "w", suffix=".json", delete=False, encoding="utf-8")
                json.dump(schema, schema_file)
                schema_file.close()
                cmd += ["--json-schema", schema_file.name]

            # Oversized prompts go on stdin rather than argv.
            if len(prompt) <= ARGV_PROMPT_LIMIT:
                cmd += ["-p", prompt]
                stdin_data = None
            else:
                cmd += ["-p", "-"]
                stdin_data = prompt

            try:
                # Run from a neutral directory: agy is a coding agent, and pointed at
                # the project it ingests workspace context it has no use for here
                # (measured: 78s from the project dir vs 50s from an empty one).
                r = subprocess.run(cmd, input=stdin_data, capture_output=True,
                                   text=True, timeout=self.timeout + 60,
                                   cwd=tempfile.gettempdir())
            except subprocess.TimeoutExpired:
                self.log(f"    ! agy timed out after {self.timeout}s")
                return None
            except OSError as exc:
                self.log(f"    ! agy could not start: {exc}")
                return None

            if r.returncode != 0:
                self.log(f"    ! agy exit {r.returncode}: {(r.stderr or '')[:200]}")
                return None

            self.calls += 1
            envelope = _extract_json(r.stdout)
            if envelope is None:
                self.log("    ! agy returned unparsable stdout")
                self._dump_envelope(r.stdout)
                return None
            # Headless JSON wraps the answer in {conversation_id, status, response, ...};
            # a --json-schema run may return the payload directly.
            if isinstance(envelope, dict) and "response" in envelope:
                status = str(envelope.get("status") or "").lower()
                if status not in ("", "ok", "success", "completed"):
                    self.log(f"    ! agy status={envelope.get('status')} "
                             f"{str(envelope.get('error'))[:120]}")

                # agy already parses the schema-constrained answer for us. Reading the
                # `response` string instead was the main cause of slow runs: it arrives
                # fence-wrapped (```json ... ```) and as a bare array rather than the
                # object the schema asks for, so successful batches looked like failures
                # and fell through to the rate-limited API.
                structured = envelope.get("structured_output")
                if structured not in (None, "", {}, []):
                    return structured

                payload = envelope["response"]
                if not payload:
                    # SUCCESS with an empty response means the agent attempted a tool
                    # call that headless mode auto-denied; the reason is on stderr.
                    reason = (r.stderr or "").strip().splitlines()
                    self.log("    ! agy returned an empty response"
                             + (f": {reason[0][:160]}" if reason else ""))
                    return None
                parsed = _extract_json(payload) if isinstance(payload, str) else payload
                if parsed is None:
                    self._dump_envelope(r.stdout)
                return parsed
            return envelope
        finally:
            if schema_file:
                try:
                    os.unlink(schema_file.name)
                except OSError:
                    pass


class GeminiAPI:
    """Fallback. Rotates the model preference list because no single free-tier model
    sustained the load in testing."""

    name = "gemini_api"
    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"

    def __init__(self, cfg: dict, api_key: str, quota=None, log=print):
        self.models = cfg.get("model_preference") or ["gemini-3.5-flash-lite"]
        self.api_key = api_key
        self.quota = quota
        self.log = log
        self.last_model = None
        self.calls = 0
        self.session = requests.Session()

    def available(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("your-")

    def preflight(self) -> tuple[bool, str]:
        return (True, "ok") if self.available() else (False, "no GEMINI_API_KEY")

    def complete(self, prompt: str, schema: dict | None = None) -> object | None:
        if not self.available():
            return None
        gen: dict = {"temperature": 0}
        if schema:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = schema

        for model in self.models:
            for attempt in range(3):
                if self.quota is not None and not self.quota.check_and_reserve("gemini", 1):
                    return None
                try:
                    r = self.session.post(
                        self.ENDPOINT.format(m=model),
                        headers={"x-goog-api-key": self.api_key,
                                 "Content-Type": "application/json"},
                        json={"contents": [{"parts": [{"text": prompt}]}],
                              "generationConfig": gen},
                        timeout=180)
                except requests.RequestException as exc:
                    self.log(f"    ! {model}: {type(exc).__name__}")
                    if self.quota is not None:
                        self.quota.refund("gemini", 1)
                    time.sleep(2 * (attempt + 1))
                    continue
                if r.status_code in (429, 503):
                    time.sleep(2 * (attempt + 1))
                    continue
                if not r.ok:
                    self.log(f"    ! {model}: HTTP {r.status_code} {r.text[:120]}")
                    break
                self.calls += 1
                self.last_model = model
                try:
                    return _extract_json(
                        r.json()["candidates"][0]["content"]["parts"][0]["text"])
                except (KeyError, IndexError, ValueError, TypeError):
                    self.log(f"    ! {model}: unusable response shape")
                    break
        self.log("    ! every Gemini model failed for this call")
        return None


class Chain:
    """Primary backend, falling back to the secondary. Records what actually answered."""

    def __init__(self, primary, secondary=None, log=print):
        self.primary = primary
        self.secondary = secondary
        self.log = log
        self.usage: dict[str, int] = {}

    def _note(self, backend):
        key = f"{backend.name}:{backend.last_model or 'default'}"
        self.usage[key] = self.usage.get(key, 0) + 1

    def complete(self, prompt: str, schema: dict | None = None) -> object | None:
        for backend in (self.primary, self.secondary):
            if backend is None:
                continue
            out = backend.complete(prompt, schema)
            if out is not None:
                self._note(backend)
                return out
        return None

    def summary(self) -> str:
        if not self.usage:
            return "no LLM calls"
        return ", ".join(f"{k} x{v}" for k, v in sorted(self.usage.items()))


def build(cfg: dict, env: dict, quota=None, log=print) -> Chain:
    """Assemble the chain from config, degrading loudly rather than failing."""
    lcfg = cfg.get("llm", {}) or {}
    want = lcfg.get("backend", "antigravity")

    agy = AntigravityCLI(lcfg.get("antigravity", {}) or {}, quota=quota, log=log)
    api = GeminiAPI(lcfg.get("gemini_api", {}) or {},
                    env.get("GEMINI_API_KEY", ""), quota=quota, log=log)

    if want == "antigravity":
        ok, why = agy.preflight()
        if ok:
            log(f"  LLM: antigravity CLI ({agy.model or 'account default'}), "
                f"Gemini API as fallback")
            return Chain(agy, api if api.available() else None, log=log)
        log(f"  ! antigravity CLI unusable ({why}) — falling back to the Gemini API")
        log("    fix: curl -fsSL https://antigravity.google/cli/install.sh | bash && agy")
    return Chain(api, None, log=log)
