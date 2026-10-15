"""Provision a named reviewer; no staff/admin privileges and no password in command output."""

import os
import secrets
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Create a personal evidence-review account and a private credentials file."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--credentials-file", required=True)

    def handle(self, *args, **options):
        username = options["username"]
        target = Path(options["credentials_file"]).resolve()
        if get_user_model().objects.filter(username=username).exists():
            raise CommandError("账号已存在；不会重置密码或覆盖其权限。")
        if target.exists():
            raise CommandError("凭据文件已存在；不会覆盖。")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        password = secrets.token_urlsafe(24)
        with transaction.atomic():
            user = get_user_model().objects.create_user(
                username=username, password=password
            )
            user.full_clean()
            user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label="curation", codename="change_evidence"
                )
            )
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(f"username: {username}\npassword: {password}\n")
                stream.flush()
                os.fsync(stream.fileno())
        self.stdout.write(f"已创建审阅账号 {username}；凭据仅保存在 {target}")
