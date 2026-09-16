from qmemo_radar.domain import PublicationPackage
from qmemo_radar.exceptions import PublishingDisabled


class DisabledQuotePublisher:
    async def publish(self, package: PublicationPackage) -> str:
        raise PublishingDisabled(
            f"Quote Memorial publishing is disabled for package {package.package_id}"
        )


class DisabledXPublisher:
    async def publish(self, package: PublicationPackage, qmemo_url: str) -> str:
        raise PublishingDisabled(f"X publishing is disabled for package {package.package_id}")

