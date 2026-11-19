#!/usr/bin/env python3
"""Upload a local artifact to the project S3 sharing prefix and print a presigned GET URL.

Copies (PutObject) from the Code Interpreter / shell local filesystem into:
  s3://{S3_BUCKET}/artifacts/{actor_id}/...

Then returns a short-lived presigned GET URL the browser can download.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
from typing import Optional, Tuple
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

_SESSION_MOUNT = "/mnt/workspace"
_ALLOWED_PREFIXES = ("artifacts", "images", "docs")
_PRESIGN_EXPIRES = max(60, int(os.environ.get("ARTIFACT_PRESIGN_GET_EXPIRES", str(12 * 3600))))


def _load_sidecar_config() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(here, "..", "config.json"),
        os.path.join(here, "config.json"),
    ):
        try:
            with open(os.path.normpath(path), "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"warning: failed to load {path}: {e}", file=sys.stderr)
    return {}


def _sanitize_actor(actor_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\-]", "_", (actor_id or "").strip()) or "user"


def _content_type(key: str) -> str:
    guessed, _ = mimetypes.guess_type(key)
    return guessed or "application/octet-stream"


def _basename(filepath: str) -> str:
    name = os.path.basename(filepath.replace("\\", "/").rstrip("/")) or "upload.bin"
    return re.sub(r"[^A-Za-z0-9._\-]", "_", name.replace("..", "_")) or "upload.bin"


def _infer_actor_and_rest(filepath: str, actor_id: Optional[str]) -> Tuple[str, str, str]:
    """Return (actor, dest_prefix, rest_under_prefix)."""
    path = os.path.abspath(os.path.expanduser(filepath)).replace("\\", "/")
    mount = (
        os.environ.get("SESSION_STORAGE_DIR")
        or os.environ.get("S3_FILES_MOUNT_PATH")
        or _SESSION_MOUNT
    ).rstrip("/")
    user = _sanitize_actor(actor_id or "")

    rel = path
    if path.startswith(f"{mount}/"):
        rel = path[len(mount) + 1 :]
    elif path.startswith(f"{_SESSION_MOUNT}/"):
        rel = path[len(_SESSION_MOUNT) + 1 :]
    elif path.startswith("/tmp/outputs/"):
        return user or "user", "artifacts", path[len("/tmp/outputs/") :] or _basename(path)
    elif path.startswith("/tmp/"):
        return user or "user", "artifacts", _basename(path)

    parts = [p for p in rel.split("/") if p]

    # /mnt/workspace/{user}/artifacts|images|docs/...
    if len(parts) >= 2 and parts[1] in _ALLOWED_PREFIXES:
        inferred_user = _sanitize_actor(parts[0])
        prefix = parts[1]
        rest = "/".join(parts[2:]) or _basename(path)
        return user or inferred_user, prefix, rest

    for prefix in _ALLOWED_PREFIXES:
        if parts and parts[0] == prefix:
            rest_parts = parts[1:]
            if user and rest_parts and rest_parts[0] == user:
                rest_parts = rest_parts[1:]
            rest = "/".join(rest_parts) or _basename(path)
            return user or "user", prefix, rest

    return user or "user", "artifacts", _basename(path)


def share_artifact(
    filepath: str,
    actor_id: Optional[str] = None,
    bucket: Optional[str] = None,
    region: Optional[str] = None,
    expires_in: int = _PRESIGN_EXPIRES,
) -> dict:
    path = os.path.abspath(os.path.expanduser(filepath))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Local file not found: {path}")

    cfg = _load_sidecar_config()
    bucket = (
        bucket
        or os.environ.get("S3_BUCKET")
        or os.environ.get("AWS_S3_BUCKET")
        or os.environ.get("STORAGE_BUCKET")
        or cfg.get("s3_bucket")
        or ""
    ).strip()
    if not bucket:
        raise RuntimeError(
            "S3_BUCKET is not set (harness env or skills/doc-sharing/config.json)"
        )

    region = (
        region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or cfg.get("region")
        or "us-west-2"
    )

    actor, prefix, rest = _infer_actor_and_rest(path, actor_id)
    dest_key = f"{prefix}/{actor}/{rest}".replace("//", "/")
    content_type = _content_type(dest_key)

    s3 = boto3.client("s3", region_name=region)
    try:
        s3.upload_file(path, bucket, dest_key, ExtraArgs={"ContentType": content_type})
    except ClientError as e:
        raise RuntimeError(f"S3 upload failed s3://{bucket}/{dest_key}: {e}") from e

    # Browser-safe download: short-lived presigned GET (ai-portal CF does not serve /artifacts).
    filename = _basename(dest_key)
    try:
        download_url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": dest_key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
                "ResponseContentType": content_type,
            },
            ExpiresIn=int(expires_in),
        )
    except ClientError as e:
        raise RuntimeError(f"Presign failed s3://{bucket}/{dest_key}: {e}") from e

    return {
        "ok": True,
        "bucket": bucket,
        "key": dest_key,
        "url": download_url,
        "download_url": download_url,
        "expires_in": int(expires_in),
        "actor_id": actor,
        "local_path": path,
        "content_type": content_type,
        "size": os.path.getsize(path),
        "s3_uri": f"s3://{bucket}/{quote(dest_key, safe='/')}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Share a local artifact via S3 presigned GET URL"
    )
    parser.add_argument(
        "--filepath", "-f", required=True,
        help="Local absolute path (/tmp/outputs/… or ARTIFACTS_DIR/…)",
    )
    parser.add_argument(
        "--actor-id", "-a",
        default=os.environ.get("ACTOR_ID") or os.environ.get("actor_id") or "",
        help="Actor / user id",
    )
    parser.add_argument("--bucket", default="", help="Override S3_BUCKET")
    parser.add_argument("--region", default="", help="Override AWS_REGION")
    parser.add_argument(
        "--expires-in", type=int, default=_PRESIGN_EXPIRES,
        help=f"Presigned URL lifetime seconds (default {_PRESIGN_EXPIRES})",
    )
    args = parser.parse_args()

    try:
        result = share_artifact(
            filepath=args.filepath,
            actor_id=args.actor_id or None,
            bucket=args.bucket or None,
            region=args.region or None,
            expires_in=args.expires_in,
        )
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
