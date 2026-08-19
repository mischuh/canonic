"""Redshift IAM credential provider (``provider:aws-iam-redshift``).

Redshift issues database credentials on demand against an IAM identity instead of a
stored password. The credential is valid for roughly 15–60 minutes, so it has to be
re-fetched over the life of a long-running daemon — which is exactly what the
``provider:`` scheme exists for.

Two Redshift flavors, one provider name:

Provisioned cluster
    ``redshift:GetClusterCredentials``, selected by ``params.cluster_id``.

Serverless workgroup
    ``redshift-serverless:GetCredentials``, selected by ``params.workgroup_name``.

Both return a *temporary user* alongside the password. That user (``IAM:canonic_ro``,
``IAMA:…``) is what the warehouse authenticates, not the configured ``db_user``, so it
travels back on :attr:`~canonic.credentials.ResolvedCredential.username` and the
connector applies it per connect.

``boto3`` is an optional dependency: only projects using this provider need it, so it
is imported at fetch time with a clear install hint rather than at module import.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Any

from canonic.credentials import ResolvedCredential
from canonic.exc import CredentialError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["make_aws_iam_redshift_provider"]

#: Requested credential lifetime. AWS clamps to its own bounds (900–3600s provisioned).
_DEFAULT_DURATION_SECONDS = 3600


def _require_boto3() -> Any:
    """Import boto3, translating its absence into an actionable configuration error."""
    try:
        import boto3
    except ImportError as exc:
        raise CredentialError(
            "credentials_ref 'provider:aws-iam-redshift' requires boto3, which is not "
            "installed. Install it with: pip install boto3"
        ) from exc
    return boto3


def _boto_errors() -> tuple[type[BaseException], ...]:
    """The botocore error families a fetch can fail with.

    botocore ships with boto3, so this only runs once :func:`_require_boto3` succeeded.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    return (BotoCoreError, ClientError)


class _RedshiftIamProvider(ABC):
    """Shared shape of the two Redshift IAM flavors.

    Subclasses differ only in which AWS API they call and how they unpack it; the
    parameter handling, error translation and result shaping are identical.
    """

    def __init__(self, params: Mapping[str, Any]) -> None:
        self._region: str | None = params.get("region")
        self._database: str | None = params.get("dbname") or params.get("database")
        self._duration_seconds = int(params.get("duration_seconds", _DEFAULT_DURATION_SECONDS))

    def get(self) -> ResolvedCredential:
        """Fetch a fresh temporary user and password from AWS."""
        boto3 = _require_boto3()
        client = boto3.client(self._service_name(), region_name=self._region)
        try:
            response = self._call(client)
        except _boto_errors() as exc:
            raise CredentialError(
                f"{self._describe()} failed to issue a Redshift credential: {exc}"
            ) from exc
        return self._to_credential(response)

    @abstractmethod
    def _service_name(self) -> str:
        """The boto3 client name to build."""

    @abstractmethod
    def _call(self, client: Any) -> Mapping[str, Any]:
        """Perform the credential-issuing API call."""

    @abstractmethod
    def _describe(self) -> str:
        """Short human-readable identification of this provider, used in errors."""

    @abstractmethod
    def _to_credential(self, response: Mapping[str, Any]) -> ResolvedCredential:
        """Unpack the API response into a :class:`ResolvedCredential`."""

    @staticmethod
    def _expiry(value: object) -> datetime | None:
        """Normalize an AWS expiration field, which boto3 returns tz-aware."""
        return value if isinstance(value, datetime) else None


class _ProvisionedClusterProvider(_RedshiftIamProvider):
    """``GetClusterCredentials`` against a provisioned Redshift cluster."""

    def __init__(self, params: Mapping[str, Any]) -> None:
        super().__init__(params)
        self._cluster_id: str = params["cluster_id"]
        db_user = params.get("db_user") or params.get("user")
        if not db_user:
            raise CredentialError(
                "provider:aws-iam-redshift on a provisioned cluster requires "
                "params.db_user (the database user IAM issues credentials for)"
            )
        self._db_user: str = db_user
        self._auto_create = bool(params.get("auto_create", False))

    def _service_name(self) -> str:
        return "redshift"

    def _describe(self) -> str:
        return f"redshift:GetClusterCredentials for cluster {self._cluster_id!r}"

    def _call(self, client: Any) -> Mapping[str, Any]:
        request: dict[str, Any] = {
            "ClusterIdentifier": self._cluster_id,
            "DbUser": self._db_user,
            "DurationSeconds": self._duration_seconds,
            "AutoCreate": self._auto_create,
        }
        if self._database:
            request["DbName"] = self._database
        response: dict[str, Any] = client.get_cluster_credentials(**request)
        return response

    def _to_credential(self, response: Mapping[str, Any]) -> ResolvedCredential:
        return ResolvedCredential(
            value=response["DbPassword"],
            expires_at=self._expiry(response.get("Expiration")),
            username=response.get("DbUser"),
        )


class _ServerlessWorkgroupProvider(_RedshiftIamProvider):
    """``redshift-serverless:GetCredentials`` against a serverless workgroup."""

    def __init__(self, params: Mapping[str, Any]) -> None:
        super().__init__(params)
        self._workgroup_name: str = params["workgroup_name"]

    def _service_name(self) -> str:
        return "redshift-serverless"

    def _describe(self) -> str:
        return f"redshift-serverless:GetCredentials for workgroup {self._workgroup_name!r}"

    def _call(self, client: Any) -> Mapping[str, Any]:
        request: dict[str, Any] = {
            "workgroupName": self._workgroup_name,
            "durationSeconds": self._duration_seconds,
        }
        if self._database:
            request["dbName"] = self._database
        response: dict[str, Any] = client.get_credentials(**request)
        return response

    def _to_credential(self, response: Mapping[str, Any]) -> ResolvedCredential:
        return ResolvedCredential(
            value=response["dbPassword"],
            expires_at=self._expiry(response.get("expiration")),
            username=response.get("dbUser"),
        )


def make_aws_iam_redshift_provider(params: Mapping[str, Any]) -> _RedshiftIamProvider:
    """Build the Redshift IAM provider matching ``params``.

    ``cluster_id`` selects the provisioned flavor, ``workgroup_name`` the serverless
    one. Naming both, or neither, is a configuration error rather than a guess.
    """
    has_cluster = bool(params.get("cluster_id"))
    has_workgroup = bool(params.get("workgroup_name"))
    if has_cluster and has_workgroup:
        raise CredentialError(
            "provider:aws-iam-redshift accepts params.cluster_id (provisioned) or "
            "params.workgroup_name (serverless), not both"
        )
    if has_cluster:
        return _ProvisionedClusterProvider(params)
    if has_workgroup:
        return _ServerlessWorkgroupProvider(params)
    raise CredentialError(
        "provider:aws-iam-redshift requires params.cluster_id (provisioned cluster) "
        "or params.workgroup_name (serverless workgroup)"
    )
