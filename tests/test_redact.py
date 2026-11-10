"""agency_loop.ingest.redact: masking rules, placeholder numbering, origin split, find_secrets (ARCHITECTURE §3.3)."""

import time
import unittest

from agency_loop.ingest.parser import parse_session
from agency_loop.ingest.redact import Redactor, find_secrets
from tests.helpers import fixture_names, load_bytes

MASKED = [
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n-----END RSA PRIVATE KEY----- 끝", "[PRIVATE_KEY] 끝"),
    ("key: -----BEGIN OPENSSH PRIVATE KEY-----\nAAAA 잘린 블록", "key: [PRIVATE_KEY]"),
    ("키 sk-ant-api03-abcdefghijklmnop 사용", "키 [SECRET_1] 사용"),
    ("sk-proj-abcdefghijklmnopqrstuv", "[SECRET_1]"),
    ("id AKIAABCDEFGHIJKLMNOP", "id [SECRET_1]"),
    ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "[SECRET_1]"),
    ("github_pat_11ABCDEFG0123456789_abcdef", "[SECRET_1]"),
    ("xoxb-1234567890-abcdef", "[SECRET_1]"),
    ("AIza" + "A" * 35, "[SECRET_1]"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", "[SECRET_1]"),
    ("Authorization: Bearer abcdef123456", "Authorization: Bearer [SECRET_1]"),
    ("password=hunter2hunter2 로 접속", "password=[SECRET_1] 로 접속"),
    ('{"api_key": "abc123def456"}', '{"api_key": "[SECRET_1]"}'),
    ("DB_PASSWORD: s3cr3t-value", "DB_PASSWORD: [SECRET_1]"),
    ("x-api-key = 'qwerty123456'", "x-api-key = '[SECRET_1]'"),
    # Key names (§3.3): a secret part or pair (split on _ - . and camelCase) unless the last part is a non-secret
    # suffix, or a whole name ending with password/passwd/secret/token.
    ("API_KEY=abcd1234efgh", "API_KEY=[SECRET_1]"),  # dotenv line
    ("access_token: abcdef123456", "access_token: [SECRET_1]"),
    ("const apiKey = 'abcdef123456';", "const apiKey = '[SECRET_1]';"),
    ("SECRET_KEY = 'django-insecure-0abc'", "SECRET_KEY = '[SECRET_1]'"),
    ("PGPASSWORD=pgpass12345", "PGPASSWORD=[SECRET_1]"),
    ("SECRET_KEY_BASE=a1b2c3d4e5f6a7b8", "SECRET_KEY_BASE=[SECRET_1]"),
    ("AWS_SECRET_ACCESS_KEY=FAKEawsSecret0123456789", "AWS_SECRET_ACCESS_KEY=[SECRET_1]"),
    ('credentials = "fake-credential-value"', 'credentials = "[SECRET_1]"'),
    ("GITHUBTOKEN=abcdef123456", "GITHUBTOKEN=[SECRET_1]"),
    ("private_key = 'abcdef123456'", "private_key = '[SECRET_1]'"),
    ("api.key: abcdef123456", "api.key: [SECRET_1]"),
    ("--token=abcdef123456", "--token=[SECRET_1]"),
    ("SG.abcdefghijklmnop1234.ABCDEFGHIJKLMNOPqrstuv0123456789", "[SECRET_1]"),  # SendGrid-style value
    ("SENDGRID=SG.abcdefghijklmnop1234.ABCDEFGHIJKLMNOPqrstuv0123456789", "SENDGRID=[SECRET_1]"),
    # Key forms: key: type = v, "key": v, cfg["key"] = v, key == v (and !=, ===, :=).
    ('api_key: str = "abcdef123456"', 'api_key: str = "[SECRET_1]"'),
    ('db_password: Optional[str] = "abcdef123456"', 'db_password: Optional[str] = "[SECRET_1]"'),
    ('"token": abcdef123456', '"token": [SECRET_1]'),
    ('cfg["api_key"] = "abcdef123456"', 'cfg["api_key"] = "[SECRET_1]"'),
    ("os.environ['GITHUB_TOKEN'] = 'abcdef123456'", "os.environ['GITHUB_TOKEN'] = '[SECRET_1]'"),
    ('if token == "abcdef123456":', 'if token == "[SECRET_1]":'),
    ('if (accessToken !== "abcdef123456")', 'if (accessToken !== "[SECRET_1]")'),
    ('token := "abcdef123456"', 'token := "[SECRET_1]"'),
    ('password = "pa.ss(word)1"', 'password = "[SECRET_1]"'),  # a quoted literal is not code, dots or not
    ("token: dXNlcjpwYXN= # 주석", "token: [SECRET_1] # 주석"),  # base64 "=" is not a `key: type = v` form
    ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "Authorization: Basic [SECRET_1]"),
    ('headers = {"Authorization": "Basic dXNlcjpwYXNz"}', 'headers = {"Authorization": "Basic [SECRET_1]"}'),
    ("https://admin:pa55word@db.example.com/x", "https://[CREDENTIAL]@db.example.com/x"),
    ("메일 alice.kim@example.com으로", "메일 [EMAIL_1]으로"),
    ("서버 203.0.113.7:8000", "서버 [IP_1]:8000"),
    ("전화 010-1234-5678", "전화 [PHONE_1]"),
    ("+82 10 1234 5678", "[PHONE_1]"),
    ("02-123-4567", "[PHONE_1]"),
    ("01012345678", "[PHONE_1]"),
    ("/home/student/app/a.py", "<HOME>/app/a.py"),
    ("cd /Users/alice", "cd <HOME>"),
    ("C:\\Users\\bob\\proj", "<HOME>\\proj"),
]

KEPT = [
    "REDIS_HOST=127.0.0.1", "listen 0.0.0.0:80", "Python 3.12.3", "weather-web@0.1.0", "token: 1234",
    "WEATHER_TOKEN=your_token_here", "export WEATHER_API_KEY=<발급받은 키>", 'api_key = os.environ.get("WEATHER_API_KEY")',
    "password: ${DB_PASSWORD}", "secret=xxxxxxxx", "max 999.1.1.1", "@pytest.fixture", "user@localhost", "타임아웃 3초",
    "/srv/home/app", "Bearer <token>",
    # Not secret key names: no secret part, or a non-secret last part.
    "max_tokens=128000", "maxTokens: 128000", "token_count = 123456", "tokenizer=bert-base-uncased",
    "token_count=123456", 'token_url="https://auth.example.com/oauth/token"', "secret_name=prod-db-secret",
    "TOKEN_TTL=86400000", "private_key_path=keys/id_rsa_fake", "PASSWORD_MIN_LENGTH=123456",
    # Code expressions and None/True/False/null are not secret values.
    "api_key = settings.api_key", "token = get_token()", 'password = config["db_password"]',
    "secret: str = load_secret(name)", "password = None", '"token": null', "access_token=False", "secret = True",
    "Basic authentication is enabled",
]


class RedactorTest(unittest.TestCase):
    def test_each_category_is_masked(self):
        for raw, expected in MASKED:
            with self.subTest(raw=raw):
                self.assertEqual(Redactor().redact(raw, origin="text"), expected)

    def test_placeholders_and_ordinary_values_are_kept(self):
        for raw in KEPT:
            with self.subTest(raw=raw):
                redactor = Redactor()
                self.assertEqual(redactor.redact(raw, origin="text"), raw)
                self.assertEqual(redactor.stats.in_text, {})
                self.assertEqual(find_secrets(raw), [])  # the send gate agrees with the masking

    def test_same_value_gets_the_same_placeholder_within_a_session(self):
        redactor = Redactor()
        self.assertEqual(redactor.redact("a@example.com, b@example.com", origin="text"), "[EMAIL_1], [EMAIL_2]")
        self.assertEqual(redactor.redact("A@Example.com 다시", origin="output"), "[EMAIL_1] 다시")
        self.assertEqual(redactor.redact("pwd=hunter22 pwd=other999 pwd=hunter22", origin="text"),
                         "pwd=[SECRET_1] pwd=[SECRET_2] pwd=[SECRET_1]")
        self.assertEqual(Redactor().redact("b@example.com", origin="text"), "[EMAIL_1]")  # numbering is per session

    def test_counts_split_by_origin(self):
        redactor = Redactor()
        redactor.redact("a@example.com /home/kim/x", origin="text")
        redactor.redact("password=hunter2hunter2 a@example.com", origin="output")
        self.assertEqual(redactor.stats.in_text, {"email": 1, "home_path": 1})
        self.assertEqual(redactor.stats.in_outputs, {"secret": 1, "email": 1})
        with self.assertRaises(KeyError):
            redactor.redact("x", origin="log")

    def test_secrets_fixture_counts_text_and_outputs_separately(self):
        stats = parse_session(load_bytes("secrets.jsonl")).redaction
        self.assertEqual(stats.in_text, {"secret": 6, "email": 4, "phone": 1, "home_path": 3, "ip": 1, "url_credential": 1})
        self.assertEqual(stats.in_outputs, {
            "home_path": 2, "private_key": 1, "secret": 3, "url_credential": 1, "email": 1, "ip": 1,
        })

    def test_long_runs_stay_linear(self):
        started = time.perf_counter()
        for text in ("a" * 1_000_000, "a+" * 500_000, "token" * 200_000, ("x" * 63 + "@") * 15_000, "1." * 500_000):
            Redactor().redact(text, origin="output")
            find_secrets(text)
        self.assertLess(time.perf_counter() - started, 10)  # quadratic patterns would take minutes


class FindSecretsTest(unittest.TestCase):
    def test_finds_raw_secrets_only(self):
        raw = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY----- password=hunter2hunter2 https://u:p@h.example"
        self.assertEqual(find_secrets(raw), ["private_key", "secret", "url_credential"])
        self.assertEqual(find_secrets("a@example.com 203.0.113.7 010-1234-5678 /home/kim/x"), [])  # personal, not secret
        self.assertEqual(find_secrets("WEATHER_TOKEN=your_token_here token: 12"), [])
        self.assertEqual(find_secrets("curl -H 'authorization: basic dXNlcjpwYXNz' x"), ["secret"])

    def test_redacted_text_is_clean(self):
        for raw, _ in MASKED:
            with self.subTest(raw=raw):
                self.assertEqual(find_secrets(Redactor().redact(raw, origin="text")), [])
        self.assertEqual(
            find_secrets(load_bytes("secrets.jsonl").decode()), ["private_key", "secret", "url_credential"]
        )
        for name in fixture_names():
            for event in parse_session(load_bytes(name)).events:
                strings = [event.text, *(v for v in (event.tool_input or {}).values() if isinstance(v, str))]
                for edit in (event.tool_input or {}).get("edits", []):
                    strings.extend(v for v in edit.values() if isinstance(v, str))
                for value in strings:
                    with self.subTest(name=name, idx=event.idx):
                        self.assertEqual(find_secrets(value), [])


if __name__ == "__main__":
    unittest.main()
