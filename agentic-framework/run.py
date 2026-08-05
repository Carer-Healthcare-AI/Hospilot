"""Startup shim: forces WindowsSelectorEventLoopPolicy before uvicorn touches asyncio.

psycopg3 / psycopg_pool require the SelectorEventLoop on Windows.  When you
invoke 'python -m uvicorn main:app' directly, uvicorn may create the
ProactorEventLoop before main.py is imported.  Running via this script ensures
the policy is set first.
"""
import sys

if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
