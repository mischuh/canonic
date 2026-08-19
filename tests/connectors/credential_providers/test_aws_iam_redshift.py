"""Tests for the Redshift IAM credential provider (``provider:aws-iam-redshift``).

boto3 is an optional extra, so every test here fakes the client surface: the point is
the request this provider builds and how it unpacks the response, not AWS itself.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from canonic.connectors.credential_providers import aws_iam_redshift
from canonic.connectors.credential_providers.aws_iam_redshift import (
    make_aws_iam_redshift_provider,
)
from canonic.exc import CredentialError

if TYPE_CHECKING:
    from collections.abc import Mapping

_EXPIRY = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)


class _FakeAwsError(Exception):
    """Stands in for botocore's ClientError/BotoCoreError family."""


class _FakeClient:
    """Records the request it was handed and returns a canned response."""

    def __init__(self, response: Mapping[str, Any] | None = None) -> None:
        self.response = response or {}
        self.request: dict[str, Any] = {}
        self.error: Exception | None = None

    def get_cluster_credentials(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._respond(kwargs)

    def get_credentials(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._respond(kwargs)

    def _respond(self, kwargs: dict[str, Any]) -> Mapping[str, Any]:
        self.request = kwargs
        if self.error is not None:
            raise self.error
        return self.response


class _FakeBoto3:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client
        self.service_name: str | None = None
        self.region_name: str | None = None

    def client(self, service_name: str, region_name: str | None = None) -> _FakeClient:
        self.service_name = service_name
        self.region_name = region_name
        return self._client


@pytest.fixture
def fake_aws(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    client = _FakeClient()
    boto3 = _FakeBoto3(client)
    monkeypatch.setattr(aws_iam_redshift, "_require_boto3", lambda: boto3)
    monkeypatch.setattr(aws_iam_redshift, "_boto_errors", lambda: (_FakeAwsError,))
    client.boto3 = boto3  # type: ignore[attr-defined]
    return client


class TestProviderSelection:
    def test_cluster_id_selects_the_provisioned_flavor(self, fake_aws: _FakeClient) -> None:
        fake_aws.response = {"DbUser": "IAM:canonic_ro", "DbPassword": "pw", "Expiration": _EXPIRY}
        provider = make_aws_iam_redshift_provider(
            {"cluster_id": "analytics-cluster", "db_user": "canonic_ro"}
        )

        provider.get()

        assert fake_aws.boto3.service_name == "redshift"  # type: ignore[attr-defined]

    def test_workgroup_name_selects_the_serverless_flavor(self, fake_aws: _FakeClient) -> None:
        fake_aws.response = {"dbUser": "IAM:canonic_ro", "dbPassword": "pw", "expiration": _EXPIRY}
        provider = make_aws_iam_redshift_provider({"workgroup_name": "analytics-wg"})

        provider.get()

        assert fake_aws.boto3.service_name == "redshift-serverless"  # type: ignore[attr-defined]

    def test_naming_both_flavors_is_rejected(self) -> None:
        with pytest.raises(CredentialError, match="not both"):
            make_aws_iam_redshift_provider({"cluster_id": "c", "workgroup_name": "w"})

    def test_naming_neither_flavor_is_rejected(self) -> None:
        with pytest.raises(CredentialError, match="requires params.cluster_id"):
            make_aws_iam_redshift_provider({"region": "eu-central-1"})

    def test_provisioned_without_db_user_is_rejected(self) -> None:
        with pytest.raises(CredentialError, match="params.db_user"):
            make_aws_iam_redshift_provider({"cluster_id": "analytics-cluster"})


class TestProvisionedCluster:
    def test_request_carries_cluster_user_and_database(self, fake_aws: _FakeClient) -> None:
        fake_aws.response = {"DbUser": "IAM:canonic_ro", "DbPassword": "pw", "Expiration": _EXPIRY}
        provider = make_aws_iam_redshift_provider(
            {
                "cluster_id": "analytics-cluster",
                "db_user": "canonic_ro",
                "dbname": "analytics",
                "region": "eu-central-1",
                "duration_seconds": 900,
            }
        )

        provider.get()

        assert fake_aws.request == {
            "ClusterIdentifier": "analytics-cluster",
            "DbUser": "canonic_ro",
            "DbName": "analytics",
            "DurationSeconds": 900,
            "AutoCreate": False,
        }
        assert fake_aws.boto3.region_name == "eu-central-1"  # type: ignore[attr-defined]

    def test_response_maps_onto_the_resolved_credential(self, fake_aws: _FakeClient) -> None:
        # The temporary IAM user travels back alongside the password: it, not the
        # configured db_user, is what the warehouse authenticates.
        fake_aws.response = {"DbUser": "IAM:canonic_ro", "DbPassword": "pw", "Expiration": _EXPIRY}
        provider = make_aws_iam_redshift_provider(
            {"cluster_id": "analytics-cluster", "db_user": "canonic_ro"}
        )

        credential = provider.get()

        assert credential.value == "pw"
        assert credential.username == "IAM:canonic_ro"
        assert credential.expires_at == _EXPIRY

    def test_aws_failure_becomes_a_credential_error(self, fake_aws: _FakeClient) -> None:
        fake_aws.error = _FakeAwsError("AccessDenied")
        provider = make_aws_iam_redshift_provider(
            {"cluster_id": "analytics-cluster", "db_user": "canonic_ro"}
        )

        with pytest.raises(CredentialError, match="analytics-cluster.*AccessDenied"):
            provider.get()


class TestServerlessWorkgroup:
    def test_request_carries_workgroup_and_database(self, fake_aws: _FakeClient) -> None:
        fake_aws.response = {"dbUser": "IAM:canonic_ro", "dbPassword": "pw", "expiration": _EXPIRY}
        provider = make_aws_iam_redshift_provider(
            {"workgroup_name": "analytics-wg", "database": "analytics"}
        )

        provider.get()

        assert fake_aws.request == {
            "workgroupName": "analytics-wg",
            "dbName": "analytics",
            "durationSeconds": 3600,
        }

    def test_response_maps_onto_the_resolved_credential(self, fake_aws: _FakeClient) -> None:
        fake_aws.response = {"dbUser": "IAM:canonic_ro", "dbPassword": "pw", "expiration": _EXPIRY}
        provider = make_aws_iam_redshift_provider({"workgroup_name": "analytics-wg"})

        credential = provider.get()

        assert credential.value == "pw"
        assert credential.username == "IAM:canonic_ro"
        assert credential.expires_at == _EXPIRY


class TestOptionalDependency:
    def test_missing_boto3_says_how_to_install_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "boto3", None)
        with pytest.raises(CredentialError, match="pip install boto3"):
            aws_iam_redshift._require_boto3()
