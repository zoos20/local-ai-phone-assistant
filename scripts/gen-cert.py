# -*- coding: utf-8 -*-
"""Самоподписанный TLS-сертификат для туннеля ассистента (wss://:8443).

Один раз создаёт пару data/tunnel_cert.pem + data/tunnel_key.pem (срок 10
лет) с SAN: DDNS-имя ПК + localhost. Сертификат самоподписанный, телефон
пинит его SHA-256 (TOFU через LAN: GET /cert) — CA и LetsEncrypt не нужны.

Использование:
    .venv\\Scripts\\python scripts\\gen-cert.py --host mypc.dynv6.net

ensure_cert() зовётся и из tunnel.py при старте (создаёт пару, если её нет
или в SAN не хватает имён — например, добавили DDNS-домен).
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import ipaddress
import re
import sys
from pathlib import Path
from typing import Iterable

DEFAULT_DAYS = 3650          # 10 лет (план): менять сертификат не хочется


def cert_fingerprint(cert_path: str | Path) -> tuple[str, str]:
    """(hex SHA-256 через двоеточия, pin в формате OkHttp) для пиннинга.

    Pin считается от DER-формы сертификата — тот же хэш, что кладёт
    CertificatePinner("sha256/...") на телефоне. Парсим PEM чистым Python
    (base64 + hashlib), чтобы /cert в server.py не тянул cryptography.
    """
    pem = Path(cert_path).read_text(encoding="ascii")
    m = re.search(
        r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----",
        pem, re.S)
    if not m:
        raise ValueError(f"нет PEM-блока CERTIFICATE в {cert_path}")
    der = base64.b64decode("".join(m.group(1).split()))
    digest = hashlib.sha256(der).digest()
    hexs = ":".join(f"{b:02X}" for b in digest)
    pin = "sha256/" + base64.b64encode(digest).decode("ascii")
    return hexs, pin


def ensure_cert(cert_path: str | Path,
                key_path: str | Path,
                names: Iterable[str] = (),
                ips: Iterable[str] = (),
                days: int = DEFAULT_DAYS,
                force: bool = False) -> bool:
    """Создаёт пару сертификат/ключ, если она отсутствует или устарела.

    «Устарела» = в SAN нет одного из нужных имён (например, появился
    ASSISTANT_DDNS_HOSTNAME). Такой случай перегенерирует сертификат ->
    SHA-256 меняется -> телефон обновит пин по LAN (GET /cert, TOFU).

    Возвращает True, если сертификат был создан/пересоздан.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert_path, key_path = Path(cert_path), Path(key_path)
    want = sorted({str(n).strip().lower() for n in names if str(n).strip()})
    ip_list = []
    for s in ips:
        try:
            ip_list.append(ipaddress.ip_address(str(s).strip()))
        except ValueError:
            pass

    if not force and cert_path.exists() and key_path.exists():
        try:
            old = x509.load_pem_x509_certificate(cert_path.read_bytes())
            if old.not_valid_after_utc > datetime.datetime.now(
                    datetime.timezone.utc) + datetime.timedelta(days=30):
                san = old.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName).value
                have = {v.lower() for v in san.get_values_for_type(x509.DNSName)}
                have_ips = set(san.get_values_for_type(x509.IPAddress))
                if set(want) <= have and set(ip_list) <= have_ips:
                    return False        # сертификат покрывает все имена
        except Exception:               # noqa: BLE001 -- не распарсился -> заново
            pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cn = next(iter(want), "gemma-tunnel")
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    san = [x509.DNSName(n) for n in want] + [x509.IPAddress(i) for i in ip_list]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san or [x509.DNSName("localhost")]),
                       critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                       critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", action="append", default=[],
                    help="имя в SAN (можно несколько), напр. mypc.dynv6.net")
    ap.add_argument("--ip", action="append", default=[],
                    help="IP в SAN (можно несколько)")
    ap.add_argument("--force", action="store_true",
                    help="перегенерировать, даже если сертификат есть")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--dir", default=str(Path(__file__).resolve().parents[1] / "data"))
    args = ap.parse_args()

    d = Path(args.dir)
    cert, key = d / "tunnel_cert.pem", d / "tunnel_key.pem"
    try:
        created = ensure_cert(cert, key, args.host, args.ip, args.days, args.force)
    except ImportError:
        print("нет пакета cryptography: .venv\\Scripts\\pip install cryptography",
              file=sys.stderr)
        return 1
    hexs, pin = cert_fingerprint(cert)
    print(("СОЗДАН" if created else "уже есть") + f": {cert}")
    print(f"key: {key}")
    print(f"SHA-256: {hexs}")
    print(f"pin (OkHttp): {pin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
