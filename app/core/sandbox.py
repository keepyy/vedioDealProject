"""Docker 沙箱：把 ffmpeg 等外部命令放进受限容器执行。

启用时（SANDBOX_ENABLED=true）：通过 docker SDK 起一个一次性容器，
挂载存储目录，限制 CPU/内存/超时，命令结束即销毁。
关闭时：直接用本机 subprocess 执行，便于开发调试。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import List

from app.config import settings

logger = logging.getLogger(__name__)


class SandboxError(RuntimeError):
    """沙箱执行失败。"""


class Sandbox:
    def __init__(self) -> None:
        self.enabled = settings.sandbox_enabled
        self.image = settings.sandbox_image
        self.cpu = settings.sandbox_cpu_limit
        self.mem = settings.sandbox_mem_limit
        self.timeout = settings.sandbox_timeout
        self._client = None
        if self.enabled:
            try:
                import docker  # type: ignore
                self._client = docker.from_env()
                self._client.ping()
                logger.info("Docker 沙箱已就绪，镜像=%s", self.image)
            except Exception as e:  # pragma: no cover
                logger.warning("Docker 不可用，回退到本机执行: %s", e)
                self.enabled = False
                self._client = None

    def run(self, command: List[str], *, workdir: str = "/work") -> str:
        """在沙箱内执行命令，返回 stdout。

        command 为列表形式，例如 ["ffmpeg", "-i", "in.mp4", "out.mp4"]。
        workdir 是容器内的工作目录，宿主机 storage 目录会被挂载到这里。
        """
        if self.enabled and self._client is not None:
            return self._run_in_container(command, workdir)
        return self._run_local(command)

    # ---------- 容器执行 ----------
    def _run_in_container(self, command: List[str], workdir: str) -> str:
        import docker  # type: ignore
        storage = str(settings.storage_path)
        logger.info("[sandbox] %s (挂载 %s -> /work)", " ".join(command), storage)
        try:
            container = self._client.containers.run(
                image=self.image,
                command=command,
                working_dir=workdir,
                volumes={storage: {"bind": "/work", "mode": "rw"}},
                cpu_quota=int(float(self.cpu) * 100000),
                mem_limit=self.mem,
                network_mode="none",
                detach=True,
                stderr=True,
                stdout=True,
            )
        except docker.errors.ImageNotFound:
            raise SandboxError(f"沙箱镜像不存在：{self.image}，请先构建：docker build -t {self.image} docker/worker")

        try:
            res = container.wait(timeout=self.timeout)
            logs = container.logs().decode("utf-8", errors="replace")
            if res.get("StatusCode", 0) != 0:
                raise SandboxError(f"沙箱命令退出码 {res.get('StatusCode')}\n{logs[-4000:]}")
            return logs
        finally:
            try:
                container.remove(force=True)
            except Exception:
                pass

    # ---------- 本机执行（调试回退） ----------
    def _run_local(self, command: List[str]) -> str:
        exe = command[0]
        if not shutil.which(exe):
            raise SandboxError(f"本机未找到可执行文件：{exe}（且沙箱已禁用）")
        logger.info("[local] %s", " ".join(command))
        try:
            res = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise SandboxError(f"命令超时（{self.timeout}s）: {e}") from e
        if res.returncode != 0:
            raise SandboxError(f"命令退出码 {res.returncode}\n{res.stderr[-4000:]}")
        return res.stdout


sandbox = Sandbox()
