"""
Quick test for detect_easy_apply()
Usage:
    python test_detect.py <linkedin_job_url>

Example:
    python test_detect.py "https://www.linkedin.com/jobs/view/4387980268"
"""

import asyncio
import sys
import logging

logging.basicConfig(level=logging.INFO, format="  %(message)s")

from linkedin_url_extractor import detect_easy_apply


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    url = "https://www.linkedin.com/jobs/view/data-scientist-artificial-intelligence-at-ibm-4379880000/"
    if not url:
        print("\nUsage: python test_detect.py <linkedin_job_url>")
        print('Example: python test_detect.py "https://www.linkedin.com/jobs/view/1234567890"')
        sys.exit(1)

    print(f"\n{'─'*55}")
    print(f"  Testing detect_easy_apply()")
    print(f"  URL: {url}")
    print(f"{'─'*55}\n")

    result = await detect_easy_apply(url, debug=True)

    print(f"\n{'─'*55}")
    if result["easy_apply"]:
        print("  ⚡ EASY APPLY")
    else:
        print("  🌐 EXTERNAL APPLY")
        print(f"  URL: {result['apply_url'] or '(not found)'}")
    if result["error"]:
        print(f"  Error: {result['error']}")
    print(f"{'─'*55}\n")


asyncio.run(main())
