import argparse
import asyncio

from dotenv import load_dotenv

from yuqing.core.llm.factory import LLMClientFactory
from yuqing.core.llm.gateway import LLMGateway
from yuqing.core.llm.roles import ROLES


async def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=ROLES)
    args = parser.parse_args()
    reply, tokens = await LLMGateway(LLMClientFactory()).ping(args.role)
    print(f"reply={reply} tokens={tokens}")


if __name__ == "__main__":
    asyncio.run(main())


def run() -> None:
    asyncio.run(main())
