# Security

Please report sensitive vulnerabilities privately through GitHub's security reporting flow when available. Do not include serial numbers, passwords, private keys or personal installation logs in public issues.

Hazzaa Plugins writes to selected Adobe directories and may execute installers supplied by the user or the online library. Review the source and use archives from publishers you trust. Administrator access does not make archive contents trustworthy.

The application contacts `hazzaa.space` for library downloads and update checks. Missing Visual C++ runtime installation uses Microsoft's download endpoint and verifies the downloaded installer signature. Registration records remain local and are protected with Windows DPAPI.

The update verification key in `update_trust.py` is public. The desktop library client identifier is also public; neither grants publisher/upload access. Private release signing keys and server secrets are not part of this repository.
