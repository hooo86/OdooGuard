"""Faux services pour le laboratoire : serveur SMTP (port 2525) et API Telegram (port 8081).

Les messages reçus sont écrits dans le répertoire passé en argument
(mail-*.eml, telegram-*.txt) pour que lab.sh puisse les compter.
"""
import http.server
import os
import socketserver
import sys
import threading
import time
import urllib.parse

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)


class SMTPHandler(socketserver.StreamRequestHandler):
    def send(self, line):
        self.wfile.write((line + "\r\n").encode())

    def handle(self):
        self.send("220 fake ESMTP")
        data, in_data = [], False
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\r\n")
            if in_data:
                if text == ".":
                    in_data = False
                    with open(os.path.join(OUT, f"mail-{time.time_ns()}.eml"), "w") as fh:
                        fh.write("\n".join(data))
                    data = []
                    self.send("250 OK")
                else:
                    data.append(text)
                continue
            cmd = text.upper()
            if cmd.startswith("EHLO"):
                self.send("250-fake")
                self.send("250 AUTH PLAIN")
            elif cmd.startswith(("HELO", "MAIL", "RCPT", "RSET", "NOOP")):
                self.send("250 OK")
            elif cmd.startswith("AUTH"):
                self.send("235 OK")
            elif cmd == "DATA":
                in_data = True
                self.send("354 go")
            elif cmd == "QUIT":
                self.send("221 bye")
                return
            else:
                self.send("502 ?")


class TelegramHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        q = urllib.parse.parse_qs(body)
        with open(os.path.join(OUT, f"telegram-{time.time_ns()}.txt"), "w") as fh:
            fh.write(f"{self.path}\n{q.get('chat_id')}\n{q.get('text', [''])[0]}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *a):
        pass


socketserver.ThreadingTCPServer.allow_reuse_address = True
smtp = socketserver.ThreadingTCPServer(("127.0.0.1", 2525), SMTPHandler)
tg = http.server.ThreadingHTTPServer(("127.0.0.1", 8081), TelegramHandler)
threading.Thread(target=smtp.serve_forever, daemon=True).start()
tg.serve_forever()
