"""Exercise real HTTP disconnects and byte-range resume without model downloads."""

import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scripts.download_benchmark_models import download_file


class DownloadResumeTests(unittest.TestCase):
    def test_retry_keeps_bytes_received_before_disconnect(self):
        content = bytes(range(256)) * 4096
        offsets = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                offset = int(self.headers.get("Range", "bytes=0-")[6:].split("-")[0])
                offsets.append(offset)
                self.send_response(206 if offset else 200)
                self.send_header("Content-Length", str(len(content) - offset))
                if offset:
                    self.send_header("Content-Range", f"bytes {offset}-{len(content)-1}/{len(content)}")
                self.end_headers()
                end = min(offset + 131072, len(content)) if len(offsets) < 3 else len(content)
                self.wfile.write(content[offset:end])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_WR)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                partial = Path(directory) / "weights.partial"
                partial.write_bytes(content[:65536])
                download_file(f"http://127.0.0.1:{server.server_port}/weights", partial,
                              len(content), 100000000, retry_delay=0)
                self.assertEqual(partial.read_bytes(), content)
                self.assertEqual(offsets, [65536, 196608, 327680])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
