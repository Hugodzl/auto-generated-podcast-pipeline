import getpass

import keyring

KEYRING_SERVICE = "rabbit_hole"


def main() -> None:
    print("Rabbit Hole secret setup (stored in Windows Credential Manager)\n")
    plex = getpass.getpass("Plex token: ")
    keyring.set_password(KEYRING_SERVICE, "plex_token", plex)
    webhook = getpass.getpass("Discord webhook URL: ")
    keyring.set_password(KEYRING_SERVICE, "discord_webhook_url", webhook)
    print("Saved. Verify with: python -c \"import keyring; print(bool(keyring.get_password('rabbit_hole','plex_token')))\"")


if __name__ == "__main__":
    main()
