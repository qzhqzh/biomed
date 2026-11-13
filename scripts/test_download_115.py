"""Offline regression checks for the portable 115 downloader.

Run: python3 -B -m unittest discover -s scripts -p test_download_115.py -v
No real account, remote file, or network request is used.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import download_115 as downloader


class Response:
    def __init__(self, body, status=200, headers=None, fail_after=None):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(body))}
        self.fail_after = fail_after
        self.closed = False

    def read(self, size=-1):
        if self.fail_after is not None:
            remaining = self.fail_after - self.body.tell()
            if remaining <= 0:
                raise TimeoutError("simulated interrupted connection")
            size = remaining if size < 0 else min(size, remaining)
        return self.body.read(size)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected extra network request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class Client:
    cookie = "UID=111_test; CID=private-cid; SEID=private-seid"

    def __init__(self):
        self.links_requested = 0

    def link(self, entry):
        self.links_requested += 1
        return "https://download.115cdn.com/file?token=private-signed-url"


class DownloadTests(unittest.TestCase):
    body = b"0123456789abcdef"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-115-download-")
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "annotation.bin"
        self.partial = self.output.with_name(self.output.name + ".part")
        self.metadata = self.partial.with_name(self.partial.name + ".json")
        self.entry = {
            "id": "123", "pickcode": "pick123", "directory": False,
            "name": "annotation.bin", "size": len(self.body),
            "sha1": hashlib.sha1(self.body).hexdigest(),
        }
        self.client = Client()
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.contexts.enter_context(redirect_stdout(io.StringIO()))
        self.contexts.enter_context(redirect_stderr(io.StringIO()))
        self.sleep = self.contexts.enter_context(patch.object(downloader.time, "sleep"))

    def prepare_partial(self, size=5, entry=None):
        self.partial.write_bytes(self.body[:size])
        self.metadata.write_text(json.dumps(downloader.file_fingerprint(entry or self.entry)))

    def ranged(self, start, body=None, total=None):
        body = self.body[start:] if body is None else body
        return Response(body, 206, {
            "Content-Length": str(len(body)),
            "Content-Range": "bytes %d-%d/%d" % (
                start, start + len(body) - 1, len(self.body) if total is None else total,
            ),
        })

    def run_download(self, opener, **kwargs):
        with patch.object(downloader, "direct_opener", return_value=opener):
            return downloader.download(self.client, self.entry, self.output, **kwargs)

    def test_resume_uses_exact_offset_and_publishes_verified_file(self):
        self.prepare_partial(5)
        opener = Opener(self.ranged(5))
        report = self.run_download(opener)
        self.assertEqual(opener.requests[0].get_header("Range"), "bytes=5-15")
        self.assertEqual(self.output.read_bytes(), self.body)
        self.assertFalse(self.partial.exists())
        self.assertFalse(self.metadata.exists())
        self.assertEqual(report["received_bytes"], len(self.body) - 5)
        self.assertTrue(report["cloud_sha1_verified"])
        self.assertEqual(report["sha256"], hashlib.sha256(self.body).hexdigest())

    def test_server_ignoring_range_leaves_partial_untouched(self):
        self.prepare_partial(5)
        response = Response(self.body)
        with self.assertRaises(downloader.DownloadError):
            self.run_download(Opener(response))
        self.assertEqual(self.partial.read_bytes(), self.body[:5])
        self.assertFalse(self.output.exists())
        self.assertTrue(response.closed)

    def test_wrong_content_range_leaves_partial_untouched(self):
        self.prepare_partial(5)
        with self.assertRaises(downloader.DownloadError):
            self.run_download(Opener(self.ranged(4)))
        self.assertEqual(self.partial.read_bytes(), self.body[:5])
        self.assertFalse(self.output.exists())

    def test_existing_destination_is_not_overwritten(self):
        self.output.write_bytes(b"user-owned file")
        opener = Opener()
        with self.assertRaises(downloader.DownloadError):
            self.run_download(opener)
        self.assertEqual(self.output.read_bytes(), b"user-owned file")
        self.assertFalse(self.partial.exists())
        self.assertEqual(self.client.links_requested, 0)

    def test_wrong_sha1_does_not_publish_file(self):
        wrong_body = b"X" + self.body[1:]
        with self.assertRaisesRegex(downloader.DownloadError, "SHA1"):
            self.run_download(Opener(Response(wrong_body)))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.partial.read_bytes(), wrong_body)
        self.assertTrue(self.metadata.exists())

    def test_wrong_expected_sha256_does_not_publish_file(self):
        with self.assertRaisesRegex(downloader.DownloadError, "SHA256"):
            self.run_download(Opener(Response(self.body)), expected_sha256="0" * 64)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.partial.read_bytes(), self.body)

    def test_partial_from_another_remote_file_is_not_appended(self):
        other = dict(self.entry, id="456", pickcode="other-pickcode")
        self.prepare_partial(5, other)
        with self.assertRaises(downloader.DownloadError):
            self.run_download(Opener())
        self.assertEqual(self.partial.read_bytes(), self.body[:5])
        self.assertEqual(self.client.links_requested, 0)
        self.assertEqual(json.loads(self.metadata.read_text())["id"], "456")

    def test_interrupted_connection_refreshes_link_and_resumes_saved_bytes(self):
        opener = Opener(Response(self.body, fail_after=5), self.ranged(5))
        report = self.run_download(opener)
        self.assertEqual(self.output.read_bytes(), self.body)
        self.assertEqual(self.client.links_requested, 2)
        self.assertIsNone(opener.requests[0].get_header("Range"))
        self.assertEqual(opener.requests[1].get_header("Range"), "bytes=5-15")
        self.assertEqual(report["received_bytes"], len(self.body))
        self.assertEqual(report["retries"], 1)
        self.sleep.assert_called_once_with(5)

    def test_retry_exhaustion_keeps_saved_bytes_for_next_run(self):
        opener = Opener(Response(self.body, fail_after=5))
        with self.assertRaises(downloader.DownloadError):
            self.run_download(opener, attempts=1)
        self.assertEqual(self.partial.read_bytes(), self.body[:5])
        self.assertFalse(self.output.exists())
        report = self.run_download(Opener(self.ranged(5)))
        self.assertEqual(self.output.read_bytes(), self.body)
        self.assertEqual(report["started_offset"], 5)

    def test_account_cookie_and_signed_url_are_not_written_to_report(self):
        opener = Opener(Response(self.body))
        self.run_download(opener)
        headers = {name.lower(): value for name, value in opener.requests[0].header_items()}
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)
        report = self.output.with_name(self.output.name + ".download.json").read_text()
        for secret in (self.client.cookie, "private-signed-url", "private-cid", "private-seid"):
            self.assertNotIn(secret, report)
        self.assertIn("download.115cdn.com", report)

    def test_unsafe_redirect_is_rejected_before_second_request(self):
        targets = (
            "http://download.115cdn.com/file",
            "https://127.0.0.1/file",
            "https://115cdn.com.evil.example/file",
            "https://user:password@download.115cdn.com/file",
            "https://download.115cdn.com:444/file",
        )
        for target in targets:
            with self.subTest(target=target):
                error = urllib.error.HTTPError(
                    "https://download.115cdn.com/file", 302, "Found",
                    {"Location": target}, io.BytesIO(b""),
                )
                opener = Opener(error)
                with self.assertRaises(downloader.DownloadError):
                    downloader.open_download(opener, "https://download.115cdn.com/file", 0, 16, 16)
                self.assertEqual(len(opener.requests), 1)

    def test_safe_redirect_preserves_range_but_never_adds_account_cookie(self):
        error = urllib.error.HTTPError(
            "https://download.115cdn.com/file", 302, "Found",
            {"Location": "https://other.115cdn.com/file"}, io.BytesIO(b""),
        )
        opener = Opener(error, self.ranged(5))
        response, host = downloader.open_download(opener, "https://download.115cdn.com/file", 5, 11, 16)
        response.close()
        self.assertEqual(host, "other.115cdn.com")
        self.assertEqual(len(opener.requests), 2)
        for request in opener.requests:
            self.assertEqual(request.get_header("Range"), "bytes=5-15")
            self.assertIsNone(request.get_header("Cookie"))
            self.assertIsNone(request.get_header("Authorization"))

    def test_disk_write_failure_does_not_retry_network(self):
        stream = Mock()
        stream.write.side_effect = OSError("disk full")
        opener = Opener(Response(self.body))
        with patch.object(downloader, "direct_opener", return_value=opener):
            with self.assertRaisesRegex(downloader.DownloadError, "本地文件写入失败"):
                downloader.transfer(self.client, self.entry, stream, 0, len(self.body))
        self.assertEqual(self.client.links_requested, 1)
        self.sleep.assert_not_called()


class TerminalQRTests(unittest.TestCase):
    payload = "http://115.com/scan/dg-" + "0" * 40

    def test_uses_scan_payload_and_preserves_quiet_zone(self):
        stream = io.StringIO()
        with patch.object(downloader, "terminal_qr_lines", wraps=downloader.terminal_qr_lines) as encoder:
            self.assertTrue(downloader.print_terminal_qr({"qrcode": self.payload}, stream))
        encoder.assert_called_once_with(self.payload)
        lines = stream.getvalue().splitlines()[1:]
        self.assertGreater(len(lines), 10)
        self.assertEqual(len({len(line) for line in lines}), 1)
        self.assertTrue(all(line.startswith("    ") and line.endswith("    ") for line in lines))
        self.assertTrue(all(not line.strip() for line in lines[:2] + lines[-2:]))

    def test_terminal_locks_black_white_and_resets_each_line(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        with patch.dict(downloader.os.environ, {"TERM": "xterm-256color"}), patch.object(downloader.shutil, "get_terminal_size", return_value=downloader.os.terminal_size((120, 40))):
            self.assertTrue(downloader.print_terminal_qr({"qrcode": self.payload}, stream))
        lines = stream.getvalue().splitlines()[1:]
        self.assertTrue(all(line.startswith("\x1b[30;107m") and line.endswith("\x1b[0m") for line in lines))

    def test_unsupported_terminals_fall_back_to_image_link(self):
        class AsciiStream(io.StringIO):
            encoding = "ascii"
        cases = [("xterm", 10, io.StringIO()), ("dumb", 120, io.StringIO()), ("xterm", 120, AsciiStream())]
        for term, width, stream in cases:
            with self.subTest(term=term, width=width, encoding=stream.encoding):
                stream.isatty = lambda: True
                with patch.dict(downloader.os.environ, {"TERM": term}), patch.object(downloader.shutil, "get_terminal_size", return_value=downloader.os.terminal_size((width, 40))):
                    self.assertFalse(downloader.print_terminal_qr({"qrcode": self.payload}, stream))
                self.assertIn("图片链接", stream.getvalue())

    def test_invalid_payload_or_encoding_failure_does_not_abort(self):
        for payload in ("https://[invalid", "https://example.com/", ["unexpected"]):
            with self.subTest(payload=payload):
                self.assertFalse(downloader.print_terminal_qr({"qrcode": payload}, io.StringIO()))
        with patch.object(downloader, "terminal_qr_lines", side_effect=ValueError("encoding failed")):
            self.assertFalse(downloader.print_terminal_qr({"qrcode": self.payload}, io.StringIO()))


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-115-login-")
        self.addCleanup(self.temp.cleanup)
        self.session = Path(self.temp.name) / "config" / "session.json"

    def test_confirmed_scan_saves_private_session_without_printing_credentials(self):
        client = Mock()
        cookie = {"UID": "111_session", "CID": "private-cid", "SEID": "private-seid"}
        client.request.side_effect = [
            {"data": {"uid": "qr-id", "time": 123, "sign": "qr-sign"}},
            {"data": {"status": 1}},
            {"data": {"status": 2}},
            {"data": {"cookie": cookie}},
        ]
        stdout = io.StringIO()
        with patch.object(downloader, "Client", return_value=client), patch.object(downloader.time, "sleep"), patch("builtins.input", return_value=""), redirect_stdout(stdout):
            downloader.login(self.session, "alipaymini")
        saved = downloader.read_json(self.session, private=True)
        self.assertEqual(saved["app"], "alipaymini")
        self.assertEqual(self.session.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.session.parent.stat().st_mode & 0o777, 0o700)
        for secret in cookie.values():
            self.assertNotIn(secret, stdout.getvalue())
        self.assertEqual(client.request.call_args.args[0], "https://qrcodeapi.115.com/app/1.0/alipaymini/1.0/login/qrcode/")
        self.assertFalse(client.request.call_args.kwargs["authenticated"])

    def test_cancelled_scan_does_not_replace_existing_session(self):
        downloader.write_json(self.session, {"cookie": "existing-session"})
        original = self.session.read_bytes()
        client = Mock()
        client.request.side_effect = [
            {"data": {"uid": "qr-id", "time": 123, "sign": "qr-sign"}},
            {"data": {"status": -2}},
        ]
        with patch.object(downloader, "Client", return_value=client), patch.object(downloader.time, "sleep"), patch("builtins.input", return_value=""), redirect_stdout(io.StringIO()):
            with self.assertRaises(downloader.DownloadError):
                downloader.login(self.session, "alipaymini")
        self.assertEqual(self.session.read_bytes(), original)

    def test_wait_response_without_status_does_not_abort_login(self):
        client = Mock()
        client.request.side_effect = [
            {"data": {"uid": "qr-id", "time": 123, "sign": "qr-sign"}},
            {"state": 1, "code": 0, "data": {}},
            {"data": {"status": 0}},
            {"data": {"status": 2}},
            {"data": {"cookie": {"UID": "111_session", "CID": "private-cid", "SEID": "private-seid"}}},
        ]
        with patch.object(downloader, "Client", return_value=client), patch.object(downloader.time, "sleep"), patch("builtins.input", return_value=""), redirect_stdout(io.StringIO()):
            downloader.login(self.session, "alipaymini")
        self.assertEqual(downloader.read_json(self.session, private=True)["app"], "alipaymini")

    def test_confirmed_scan_without_cookie_keeps_old_session(self):
        downloader.write_json(self.session, {"cookie": "existing-session"})
        original = self.session.read_bytes()
        client = Mock()
        client.request.side_effect = [
            {"data": {"uid": "qr-id", "time": 123, "sign": "qr-sign"}},
            {"data": {"status": 2}},
            {"state": 1, "data": {}},
        ]
        with patch.object(downloader, "Client", return_value=client), patch.object(downloader.time, "sleep"), patch("builtins.input", return_value=""), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(downloader.DownloadError, "未返回.*会话"):
                downloader.login(self.session, "alipaymini")
        self.assertEqual(self.session.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
