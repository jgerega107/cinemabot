"""Acquire a dedicated user's token without putting passwords in shell history."""
import argparse
import getpass
import os

import httpx


def main():
    parser = argparse.ArgumentParser(description="Create a private environment file for a Jellyfin bot user")
    parser.add_argument("--url", required=True, help="Private Jellyfin base URL")
    parser.add_argument("--output", default=".env.jellyfin", help="New file; refuses to overwrite an existing file")
    args = parser.parse_args()
    user = input("Dedicated Jellyfin username: ")
    password = getpass.getpass("Jellyfin password: ")
    header = 'MediaBrowser Client="Fluxer Cinema", Device="Cinema", DeviceId="jellyfin-fluxer-cinema", Version="0.1.0"'
    try:
        with httpx.stream("POST", args.url.rstrip("/") + "/Users/AuthenticateByName",
                          headers={"Authorization": header, "Accept-Encoding": "identity"},
                          json={"Username": user, "Pw": password}, timeout=20, follow_redirects=False) as response:
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("Compressed authentication responses are not accepted")
            response.raise_for_status()
            response.read()
            result = response.json()
        if result["User"].get("Policy", {}).get("IsAdministrator"):
            raise ValueError("Use a dedicated non-admin Jellyfin user")
        content = f"JELLYFIN_USER_ID={result['User']['Id']}\nJELLYFIN_ACCESS_TOKEN={result['AccessToken']}\n"
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(content)
        print(f"Saved user credentials to {args.output}. Merge these two settings into your private .env.")
    except Exception as e:
        print(f"Could not save credentials ({type(e).__name__}). Check the private URL, user permissions, and output path.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
