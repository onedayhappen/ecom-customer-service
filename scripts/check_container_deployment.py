"""Build and verify the real Compose services with disposable data and offline keys.

Never reads .env values, binds existing data, or exposes host ports. Only this
run's uniquely named Compose project and volumes are removed on completion.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def run(*args, capture=False, expected=0):
    result = subprocess.run(args, cwd=ROOT, text=True, encoding="utf-8",
                            errors="replace", capture_output=capture, timeout=3600)
    if result.returncode != expected:
        if capture:
            print(result.stdout, result.stderr, flush=True)
        raise RuntimeError(f"{args[0]} exited {result.returncode}, expected {expected}")
    return result.stdout if capture else None


def isolated_config(project, backend_image=None, public_mirror=False):
    # --no-env-resolution prevents loading service env_file contents. Do not
    # reuse the application's resolved environment or its writable host binds.
    source = json.loads(run("docker", "compose", "config", "--no-env-resolution",
                            "--no-interpolate", "--format", "json", capture=True))
    services = {name: service for name, service in source["services"].items()
                if not service.get("profiles")}
    common = {
        "API_JWT_SECRET": secrets.token_hex(32), "API_AUTH_BACKEND": "mysql",
        "BUSINESS_DATA_KEYS": json.dumps({"1": base64.urlsafe_b64encode(os.urandom(32)).decode()}),
        "BUSINESS_DATA_KEY_VERSION": "1", "SUPPORT_DATABASE_URL": "",
        "API_SESSION_POSTGRES_DSN": "postgresql://postgres:postgres@postgres:5432/ecom_agent",
        "MEMORY_POSTGRES_DSN": "postgresql://postgres:postgres@postgres:5432/ecom_agent",
        "MEMORY_STORAGE_BACKEND": "postgres", "MEMORY_REDIS_URL": "redis://redis:6379/0",
        "API_CONCURRENCY_REDIS_URL": "redis://redis:6379/0",
        "ORDER_DB_DSN": "mysql://ecom:ecom_dev_password@mysql:3306/ecom_agent_orders",
        "ORDER_CACHE_REDIS_URL": "redis://redis:6379/0",
        "DEEPSEEK_API_KEY": "deployment-offline-key", "DEEPSEEK_BASE_URL": "http://127.0.0.1:9/v1",
        "MCP_ENABLED": "false", "COMMERCE_PROVIDER_MODE": "simulator",
        "COMMERCE_INVOICE_ROOT": "/app/data/demo-invoices",
    }
    targets = {"/app/data": "app", "/app/logs": "logs", "/var/lib/mysql": "mysql",
               "/var/lib/postgresql/data": "postgres", "/data": "redis"}
    volumes = {}
    for name, service in services.items():
        service.pop("env_file", None)
        service.pop("ports", None)
        service.pop("restart", None)
        environment = service.setdefault("environment", {})
        if name not in {"mysql", "postgres", "redis", "gateway", "frontend"}:
            # Isolated endpoints must win even if Compose later gains explicit
            # production DSNs. Keep only non-overridden process-specific flags.
            service["environment"] = {**environment, **common}
            service["image"] = backend_image or f"{project}-backend:local"
        if name == "frontend":
            service["image"] = f"{project}-frontend:local"
            if public_mirror:
                service["build"]["args"].update(
                    NODE_IMAGE="public.ecr.aws/docker/library/node:20-alpine",
                    NGINX_IMAGE="public.ecr.aws/docker/library/nginx:1.27-alpine")
        if name == "backend" and public_mirror:
            service["build"]["args"] = {"PYTHON_IMAGE": "public.ecr.aws/docker/library/python:3.11-slim"}
        mounts = []
        for mount in service.get("volumes", []):
            target = mount["target"]
            if target in targets:
                volume = targets[target]
                volumes[volume] = {}
                mounts.append({"type": "volume", "source": volume, "target": target})
            elif target != "/app/model":
                if not mount.get("read_only"):
                    raise RuntimeError(f"Unrecognized writable mount in {name}: {target}")
                mounts.append(mount)
        service["volumes"] = mounts
    return {"services": services, "volumes": volumes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-image", help="Reuse an explicitly built full backend image")
    parser.add_argument("--public-mirror", action="store_true", help="Use Docker Official Images on public ECR")
    args = parser.parse_args()
    project = "ecom-accept-" + uuid4().hex[:10]
    config = isolated_config(project, args.backend_image, args.public_mirror)
    with tempfile.TemporaryDirectory(prefix=project) as folder:
        path = Path(folder) / "compose.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        base = ["docker", "compose", "-p", project, "-f", str(path)]

        def compose(*command, **kwargs):
            return run(*base, *command, **kwargs)

        def sql(statement):
            return compose("exec", "-T", "mysql", "mysql", "-uroot", "-proot_dev_password",
                           "--batch", "--skip-column-names", "ecom_agent_orders", "-e", statement,
                           capture=True).strip()

        try:
            print(f"Isolated deployment acceptance: {project}", flush=True)
            compose("build", *( ["frontend"] if args.backend_image else ["backend", "frontend"] ))
            compose("run", "--rm", "--no-deps", "order-init", "python", "-m", "pip", "check")
            compose("up", "-d", "--wait", "--wait-timeout", "180", "mysql", "postgres", "redis")
            assert sql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()") == "0"
            compose("run", "--rm", "--no-deps", "order-init", "python",
                    "scripts/migrate_mysql_business_schema.py", "--check", expected=1)
            compose("run", "--rm", "--no-deps", "order-init")
            compose("run", "--rm", "--no-deps", "order-init", "python",
                    "scripts/migrate_mysql_business_schema.py", "--check")
            sql("CREATE TABLE acceptance_sentinel(id INT PRIMARY KEY); INSERT INTO acceptance_sentinel VALUES (1)")
            before = sql("SELECT version,applied_at FROM schema_migrations ORDER BY version")
            compose("run", "--rm", "--no-deps", "order-init")
            assert before == sql("SELECT version,applied_at FROM schema_migrations ORDER BY version")
            assert sql("SELECT COUNT(*) FROM acceptance_sentinel") == "1"
            # Exercise the actual dependency gate, before starting any backend.
            failed = json.loads(json.dumps(config))
            failed["services"]["order-init"]["command"] = ["python", "-c", "raise SystemExit(17)"]
            fail_path = Path(folder) / "failed.json"
            fail_path.write_text(json.dumps(failed), encoding="utf-8")
            result = subprocess.run(["docker", "compose", "-p", project, "-f", str(fail_path),
                                     "up", "-d", "backend"], cwd=ROOT, timeout=180)
            assert result.returncode != 0, "Backend started despite migration failure"
            state = json.loads(compose("ps", "--all", "--format", "json", "backend", capture=True) or "{}")
            assert state.get("State") != "running", "Failed init did not gate backend"
            compose("up", "-d", "--wait", "--wait-timeout", "180")
            compose("exec", "-T", "backend", "python", "-c",
                    "import urllib.request; [urllib.request.urlopen('http://127.0.0.1:8010/'+p) for p in ('health','ready')]")
            compose("exec", "-T", "frontend", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1/")
            compose("exec", "-T", "gateway", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1/health")
            # Check that the actual non-root worker can publish a file and the
            # API can recover the same bytes and decrypt with its matching key.
            compose("exec", "-T", "commerce-worker", "python", "-c",
                    "from pathlib import Path; from app.config.settings import settings; "
                    "from app.services.commerce.common import Cipher; "
                    "c=Cipher(settings.business_data_keys,settings.business_data_key_version); "
                    "v=c.seal('deployment-probe','acceptance'); k=settings.business_data_key_version; "
                    "p=Path(settings.commerce_invoice_root); p.mkdir(parents=True,exist_ok=True); "
                    "(p/'acceptance.json').write_text(__import__('json').dumps([v.hex(),k]))")
            compose("restart", "backend", "commerce-worker")
            for _ in range(30):
                probe = subprocess.run([*base, "exec", "-T", "backend", "python", "-c",
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/ready')"],
                    capture_output=True, timeout=10)
                if probe.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Backend did not recover after restart")
            compose("exec", "-T", "backend", "python", "-c",
                    "from pathlib import Path; import json; from app.config.settings import settings; "
                    "from app.services.commerce.common import Cipher; "
                    "v,k=json.loads((Path(settings.commerce_invoice_root)/'acceptance.json').read_text()); "
                    "assert Cipher(settings.business_data_keys,settings.business_data_key_version).open(bytes.fromhex(v),'acceptance',k)=='deployment-probe'")
            assert sql("SELECT COUNT(*) FROM acceptance_sentinel") == "1"
            assert before == sql("SELECT version,applied_at FROM schema_migrations ORDER BY version")
            time.sleep(5)
            states = compose("ps", "--all", "--format", "json", capture=True)
            rows = [json.loads(line) for line in states.splitlines() if line.strip()]
            assert all(row["State"] == "running" for row in rows if row["Service"] != "order-init"), states
            print("PASS: full images, clean schema, repeat migrations, failure gate, services, shared files/keys and restart", flush=True)
        except BaseException:
            compose("logs", "--no-color", "--tail", "50")
            raise
        finally:
            compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
