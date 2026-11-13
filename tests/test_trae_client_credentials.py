"""Trae 客户端凭据通道：tc 信封解密、JWT 解析、双客户端发现、DeviceProof"""
import base64
import json
import os
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import trae_client_credentials as tcc


def _sha512(data):
    import hashlib
    return hashlib.sha512(data).digest()


def tc_encrypt(plaintext):
    """按客户端 byteCrypto 封装格式造合法信封（与 tc_decrypt 互逆）"""
    random_bytes = os.urandom(32)
    derived = _sha512(_sha512(random_bytes) + tcc.PEPPER)
    key, iv = derived[:16], derived[16:32]
    body = _sha512(plaintext.encode("utf-8")) + plaintext.encode("utf-8")
    pad = 16 - len(body) % 16
    padded = body + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    cipher = enc.update(padded) + enc.finalize()
    return base64.b64encode(tcc.ENVELOPE_HEADER + random_bytes + cipher).decode("ascii")


def make_jwt(uid, exp):
    def b64(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "%s.%s.sig" % (b64({"alg": "RS256", "typ": "JWT"}),
                          b64({"data": {"id": uid}, "exp": exp}))


def make_storage_json(jwt, rt="rt", refresh_exp_ms=9_000_000_000, device_id="450130483890571"):
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode("utf-8")
    cloudide = {"token": jwt, "refreshToken": rt, "refreshExpiredAt": refresh_exp_ms}
    device = {"privateKeyPEM": pem}
    return json.dumps({
        "iCubeAuthInfo://icube.cloudide": tc_encrypt(json.dumps(cloudide)),
        "iCubeAuthInfo://icube-dc:%s" % device_id: tc_encrypt(json.dumps(device)),
        "telemetry.machineId": "0123456789abcdef0123456789abcdef",
    })


class TestTcEnvelope(unittest.TestCase):
    def test_roundtrip(self):
        plain = '{"token":"abc","refreshToken":"rt","nested":{"k":1}}'
        self.assertEqual(tcc.tc_decrypt(tc_encrypt(plain)), plain)

    def test_rejects_corruption(self):
        raw = bytearray(base64.b64decode(tc_encrypt("hello")))
        raw[-1] ^= 0xFF
        with self.assertRaises(ValueError):
            tcc.tc_decrypt(base64.b64encode(bytes(raw)).decode("ascii"))
        with self.assertRaises(ValueError):
            tcc.tc_decrypt("!!!not-base64!!!")


class TestJWT(unittest.TestCase):
    def test_payload(self):
        jwt = make_jwt("942709926015804", 1_799_999_999)
        self.assertEqual(tcc.jwt_payload(jwt), ("942709926015804", 1_799_999_999))
        self.assertIsNone(tcc.jwt_payload("not-a-jwt"))

    def test_usable_window(self):
        acc = tcc.ClientTraeAccount("1", make_jwt("1", 1_000), 1_000)
        self.assertTrue(tcc.jwt_usable(acc, now=800))
        self.assertFalse(tcc.jwt_usable(acc, now=900))


class TestDiscover(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        tcc.storage_override = None

    def tearDown(self):
        tcc.storage_override = None

    def _write(self, app, content):
        path = os.path.join(self.dir, app, "User", "globalStorage", "storage.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_dedupes_by_uid_and_keeps_freshest(self):
        uid = "942709926015804"
        p1 = self._write("Trae CN", make_storage_json(
            make_jwt(uid, 1_000_000), rt="rt-old"))
        p2 = self._write("Trae CN 2", make_storage_json(
            make_jwt(uid, 2_000_000), rt="rt-new"))
        tcc.storage_override = [p1, p2]
        accounts = tcc.discover()
        self.assertEqual(len(accounts), 1)
        acc = accounts[0]
        self.assertEqual(acc.uid, uid)
        self.assertEqual(acc.jwt_exp, 2_000_000)
        self.assertEqual(acc.refresh_token, "rt-new")
        self.assertEqual(acc.device_id, "450130483890571")
        self.assertIn("BEGIN", acc.device_key_pem)

    def test_ignores_missing_or_malformed(self):
        bad = os.path.join(self.dir, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        tcc.storage_override = [os.path.join(self.dir, "nope.json"), bad]
        self.assertEqual(tcc.discover(), [])

    def test_storage_urls_include_home_second_client(self):
        """第二客户端用 --user-data-dir 放在主目录（如 ~/TraeCN2）时也要能发现"""
        path = self._write("TraeCN2", make_storage_json(make_jwt("42", 99)))
        with mock.patch.object(tcc.os.path, "expanduser", return_value=self.dir):
            urls = tcc.storage_urls()
        self.assertIn(path, urls)


class TestProofAndRefresh(unittest.TestCase):
    def test_proof_message_format(self):
        msg = tcc.proof_message("/trae/api/v3/oauth/ExchangeToken", "ono9krqynydwx5",
                                "rt-token", 1_700_000_000, "abcd")
        self.assertEqual(
            msg,
            "POST\n/trae/api/v3/oauth/ExchangeToken\nono9krqynydwx5\nrt-token\n1700000000\nabcd")

    def test_nonce_is_unique_hex32(self):
        a, b = tcc.nonce(), tcc.nonce()
        self.assertEqual(len(a), 32)
        self.assertNotEqual(a, b)
        int(a, 16)

    def test_refresh_requires_full_ingredients(self):
        acc = tcc.ClientTraeAccount("1", make_jwt("1", 0), 0)
        self.assertIsNone(tcc.refresh(acc))


if __name__ == "__main__":
    unittest.main()
