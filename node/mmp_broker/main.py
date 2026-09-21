"""入口：A 节点 + 绑定模式 B-py。`mmp-node [node.toml]`。"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn

from mmp_node.config import load_config
from mmp_node.node import Node
from mmp_node.ws_client import WsLink

from .core import Broker, RegisterRejected
from .http import create_app
from .inproc import InProcLink


async def serve(cfg_path: str | None) -> None:
    cfg = load_config(cfg_path)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    node = Node(cfg)
    await node.start()
    broker = Broker(cfg.node.key, cfg.node.protocol_version, cfg.broker.inflight_grace_sec)
    link = InProcLink(node, broker)
    try:
        await link.connect()
    except RegisterRejected as e:
        logging.getLogger("mmp").error("register rejected: %s", e)
        await node.close()
        sys.exit(2)
    ws_links = [WsLink(node, ep) for ep in cfg.brokers]
    for l in ws_links:
        l.start()
    try:
        if cfg.broker.enabled:
            server = uvicorn.Server(uvicorn.Config(create_app(broker, cfg.broker.api_key), host=cfg.broker.host,
                                                   port=cfg.broker.port, log_level="info"))
            await server.serve()
        else:
            await asyncio.Event().wait()
    finally:
        for l in ws_links:
            await l.close()
        await link.close()
        await node.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="multimodal-pipeline A node + bound B-py")
    ap.add_argument("config", nargs="?", default="node.toml", help="node.toml 路径（环境变量 MMP_* 可覆盖）")
    args = ap.parse_args()
    asyncio.run(serve(args.config))


if __name__ == "__main__":
    main()
