"""Analytics responses name the destination platform for Zernio accounts."""

import pytest

from apps.analytics.api_builders import build_account_analytics
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from apps.zernio.models import ZernioAccountBinding, ZernioApiKey

pytestmark = pytest.mark.django_db

SK_KEY = "sk_" + "a" * 64


def test_zernio_account_analytics_names_destination(organization):
    workspace = Workspace.objects.create(organization=organization, name="WS")
    account = SocialAccount.objects.create(
        workspace=workspace, platform="zernio", account_platform_id="z-tt", account_name="BeritaKenapa"
    )
    key = ZernioApiKey.objects.create(workspace=workspace, label="Key 1", api_key=SK_KEY)
    ZernioAccountBinding.objects.create(
        social_account=account,
        api_key=key,
        zernio_account_id="z-tt",
        zernio_profile_id="prof1",
        destination_platform="tiktok",
    )
    response = build_account_analytics(account, 7)
    assert response.platform == "tiktok"
    assert response.transport == "zernio"
    assert response.analytics_available is False
