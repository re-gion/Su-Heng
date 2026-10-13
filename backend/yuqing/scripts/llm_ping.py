import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from yuqing.core.llm.factory import LLMClientFactory
from yuqing.core.llm.gateway import LLMGateway
from yuqing.core.llm.roles import ROLES
from yuqing.services.configuration import ConfigService
from yuqing.storage.db import Database


def parse_args() -> argparse.Namespace:
    load_dotenv(Path(__file__).parents[3] / ".env")
    parser = argparse.ArgumentParser(description="使用系统有效配置进行模型 JSON 语义连接测试")
    parser.add_argument("role", choices=ROLES)
    parser.add_argument("--data-dir", default=os.getenv("YUQING_DATA_DIR", "data"))
    args = parser.parse_args()
    args.data_dir = str(Path(args.data_dir).resolve())
    return args


async def main(args: argparse.Namespace | None = None) -> None:
    args = args or parse_args()
    database = Database(Path(args.data_dir) / "yuqing.db")
    await database.initialize()
    factory = None
    try:
        factory = LLMClientFactory(await ConfigService(database).resolved_environ())
        reply, tokens = await LLMGateway(factory).ping(args.role)
        print(f"reply={reply} tokens={tokens} model={factory.config(args.role).model}")
    finally:
        if factory is not None:
            await factory.aclose()
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())


def run() -> None:
    asyncio.run(main())
