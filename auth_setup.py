# auth_setup.py
import argparse
import json
import os.path
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate or refresh token.json for Google Drive OAuth."
    )
    parser.add_argument(
        "--force-reauth",
        action="store_true",
        help="Delete existing token.json and force a new browser auth session.",
    )
    return parser.parse_args()


def start_auth_flow():
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    return flow.run_local_server(port=0)


def update_env_token_json_from_file():
    if not os.path.exists(".env"):
        print(".env not found; skipping GOOGLE_OAUTH_TOKEN_JSON update.")
        return

    with open("token.json", "r", encoding="utf-8") as token_file:
        token_info = json.load(token_file)
    token_json_single_line = json.dumps(token_info, separators=(",", ":"))

    with open(".env", "r", encoding="utf-8") as env_file:
        lines = env_file.readlines()

    replacement_line = f"GOOGLE_OAUTH_TOKEN_JSON={token_json_single_line}\n"
    updated_lines = []
    replaced = False

    for line in lines:
        if line.strip().startswith("GOOGLE_OAUTH_TOKEN_JSON="):
            if not replaced:
                updated_lines.append(replacement_line)
                replaced = True
            continue
        updated_lines.append(line)

    if not replaced:
        if updated_lines and not updated_lines[-1].endswith("\n"):
            updated_lines[-1] = updated_lines[-1] + "\n"
        updated_lines.append(replacement_line)

    with open(".env", "w", encoding="utf-8") as env_file:
        env_file.writelines(updated_lines)

    print("Updated .env GOOGLE_OAUTH_TOKEN_JSON from token.json")


def maybe_update_env_token_json():
    answer = input(
        "Update .env GOOGLE_OAUTH_TOKEN_JSON with current token.json contents? [y/N]: "
    ).strip().lower()
    if answer in {"y", "yes"}:
        update_env_token_json_from_file()
    else:
        print("Skipped .env update.")


def main():
    args = parse_args()
    creds = None
    token_was_updated = False

    if args.force_reauth and os.path.exists("token.json"):
        os.remove("token.json")
        print("Removed token.json due to --force-reauth. Starting a new auth session...")

    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        if creds.valid:
            print("Already have valid token.json")
            return

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_was_updated = True
        except RefreshError as error:
            print(f"Failed to refresh existing token.json: {error}")
            answer = input("Remove token.json and start a new auth session? [y/N]: ").strip().lower()
            if answer in {"y", "yes"}:
                os.remove("token.json")
                print("Removed token.json. Starting a new auth session...")
                creds = start_auth_flow()
                token_was_updated = True
            else:
                print("Keeping token.json. Aborting.")
                raise SystemExit(1)
    else:
        creds = start_auth_flow()
        token_was_updated = True

    with open("token.json", "w") as f:
        f.write(creds.to_json())
    print("Wrote token.json")

    if token_was_updated:
        maybe_update_env_token_json()

if __name__ == "__main__":
    main()
