#!/usr/bin/env python3
"""Static HTTP server with single-byte-range support for HTML5 video scrubbing.

Run from the folder containing the HTML and MP4:
    py range_server.py --port 5080

Then open on the PC:
    http://localhost:5080/video_scroll_diagnostics.html
And on a phone connected to the same Wi-Fi:
    http://<PC-LAN-IP>:5080/video_scroll_diagnostics.html
"""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import random
import re
from urllib.parse import parse_qs, quote, unquote, urlsplit

PHOTO_DIR = r"D:\WEBSITE_PHOTOS"
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeRequestHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browser closed an HTTP/1.1 keep-alive connection or aborted a range stream.
            self.close_connection = True

    def end_headers(self):
        # Always advertise byte-range support, even on 200 OK responses
        if "Accept-Ranges" not in self._headers_buffer:
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def do_GET(self):
        request_path = urlsplit(self.path).path

        # Return 11 random JPG image URLs as JSON (or all if ?all=1)
        if request_path == "/api/random-images":
            try:
                jpg_files = []

                for root, _, files in os.walk(PHOTO_DIR):
                    for name in files:
                        if name.lower().endswith((".jpg", ".jpeg")):
                            full_path = os.path.join(root, name)
                            relative_path = os.path.relpath(full_path, PHOTO_DIR)
                            jpg_files.append(relative_path)

                random.shuffle(jpg_files)

                query = parse_qs(urlsplit(self.path).query)
                show_all = query.get("all", ["0"])[0] == "1"

                selected = jpg_files if show_all else jpg_files[:11]

                image_urls = [
                    "/photos/" + quote(name.replace("\\", "/"), safe="/")
                    for name in selected
                ]

                print(
                    f"[IMAGES] Found {len(jpg_files)} images; "
                    f"returning {len(selected)}"
                )

                payload = json.dumps(image_urls).encode("utf-8")

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True
            except OSError as error:
                print(f"[IMAGES] Error: {error}")
                self.send_error(500, "Unable to read photo folder")

            return

        # Preserve normal static-file and video handling
        super().do_GET()

    def translate_path(self, path):
        request_path = unquote(urlsplit(path).path)

        # Map /photos/filename.jpg to D:\WEBSITE_PHOTOS\filename.jpg
        if request_path.startswith("/photos/"):
            relative_path = request_path[len("/photos/") :]

            full_path = os.path.realpath(os.path.join(PHOTO_DIR, relative_path))
            base_path = os.path.realpath(PHOTO_DIR)

            # Prevent access outside the photo folder
            if os.path.commonpath([base_path, full_path]) != base_path:
                return os.path.join(base_path, "__invalid_photo__.jpg")

            return full_path

        return super().translate_path(path)

    def send_head(self):
        # Reset per-request state. SimpleHTTPRequestHandler calls send_head for GET/HEAD.
        self._byte_range = None
        range_value = self.headers.get("Range")
        if not range_value:
            return super().send_head()

        # This implementation intentionally supports one byte range, which is enough
        # for normal browser MP4 seeking. Fall back to default handling for other paths.
        path = self.translate_path(self.path)
        if os.path.isdir(path) or not os.path.isfile(path):
            return super().send_head()

        try:
            file_obj = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        size = os.fstat(file_obj.fileno()).st_size
        match = _RANGE_RE.match(range_value.strip())
        if not match or size <= 0:
            file_obj.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        first_text, last_text = match.groups()
        if first_text == "" and last_text == "":
            file_obj.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        if first_text == "":
            # Suffix range: bytes=-N
            suffix_length = int(last_text)
            if suffix_length <= 0:
                start, end = size, size - 1
            else:
                start, end = max(0, size - suffix_length), size - 1
        else:
            start = int(first_text)
            end = int(last_text) if last_text else size - 1
            end = min(end, size - 1)

        if start < 0 or start >= size or end < start:
            file_obj.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        self._byte_range = (start, end)
        self.send_response(206, "Partial Content")
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header(
            "Last-Modified", self.date_time_string(os.path.getmtime(path))
        )
        self.end_headers()
        print(
            f"[RANGE] {self.command} {self.path} Range={range_value!r} -> "
            f"206 bytes {start}-{end}/{size}"
        )
        return file_obj

    def copyfile(self, source, outputfile):
        byte_range = getattr(self, "_byte_range", None)

        try:
            if byte_range is None:
                return super().copyfile(source, outputfile)

            start, end = byte_range
            source.seek(start)
            remaining = end - start + 1
            chunk_size = 64 * 1024

            while remaining > 0:
                chunk = source.read(min(chunk_size, remaining))
                if not chunk:
                    break

                outputfile.write(chunk)
                remaining -= len(chunk)

        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Mark connection closed so HTTP/1.1 keep-alive doesn't read from a dead socket
            self.close_connection = True
            return

    def log_message(self, fmt, *args):
        super().log_message(fmt, *args)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Static file server with HTTP byte-range support"
    )
    parser.add_argument(
        "--port", type=int, default=5080, help="TCP port (default: 5080)"
    )
    parser.add_argument(
        "--directory",
        default=".",
        help="directory to serve (default: current directory)",
    )
    args = parser.parse_args()

    directory = os.path.abspath(args.directory)
    if not os.path.isdir(directory):
        parser.error(f"directory does not exist: {directory}")

    handler = partial(RangeRequestHandler, directory=directory)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    print(f"Serving folder: {directory}")
    print(f"PC URL:        http://localhost:{args.port}/")
    print(f"Phone URL:     http://<PC-LAN-IP>:{args.port}/")
    print(
        "Byte-range support enabled (video seeks should receive HTTP 206). "
        "Press Ctrl+C to stop."
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()