"""Public on-demand list-price lookups for AKS/EKS/GKE node pools.

These are estimates from each cloud's own public pricing catalog -- NOT the
customer's actual invoice. Excluded from every number here: committed-use
discounts, reserved instances, savings plans, spot/preemptible pricing,
storage, network egress, load balancers, and control-plane fees. See
providers.base.CostEstimate.disclaimer, which is attached to every result
and meant to be shown in the UI, not just logged.

Each cloud needed a different strategy:
  - Azure: the Retail Prices API (prices.azure.com) is public, unauthenticated,
    and returns an exact price for an exact SKU+region -- the simplest and
    most reliable of the three.
  - AWS: the Pricing API (`aws pricing get-products`) requires the specific
    filter combination AWS/OpenCost/Kubecost all use (Shared tenancy, Linux,
    no pre-installed software, "Used" capacity status) to get exactly one
    on-demand price back instead of dozens of irrelevant line items.
  - GCP: there is no single "price of this machine type" endpoint. Predefined
    machine types (e2/n1/n2/n2d-*) are billed as separate per-vCPU and
    per-GB-RAM SKUs from the Cloud Billing Catalog API, so pricing one
    machine type means fetching both SKUs and multiplying by the type's
    vCPU/RAM shape. This only covers the families in _GCP_FAMILY_SHAPE below
    (which covers the overwhelming majority of real GKE node pools); shared-
    core (e2-micro/small/medium) and less common families report as
    "unsupported" rather than guessing.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from kube_client import run_cli

logger = logging.getLogger("kubeorbit.providers.pricing")

_HTTP_TIMEOUT = 15.0


# --------------------------------------------------------------------------- #
# Azure -- public Retail Prices API, no auth required
# --------------------------------------------------------------------------- #

async def fetch_azure_vm_hourly_price(region: str, vm_size: str) -> Optional[float]:
    filt = (
        f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' "
        f"and armSkuName eq '{vm_size}' and priceType eq 'Consumption'"
    )
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get("https://prices.azure.com/api/retail/prices", params={"$filter": filt})
        resp.raise_for_status()
        items = resp.json().get("Items", [])
    except Exception as exc:
        logger.info("Azure retail price lookup failed for %s/%s: %s", region, vm_size, exc)
        return None

    # Exclude Windows (Linux is KubeOrbit's assumption for AKS Linux node
    # pools), Spot, and Low Priority meters -- keep the plain on-demand rate.
    candidates = [
        i for i in items
        if "windows" not in i.get("productName", "").lower()
        and "spot" not in i.get("meterName", "").lower()
        and "low priority" not in i.get("meterName", "").lower()
    ]
    if not candidates:
        return None
    return min(c["retailPrice"] for c in candidates)


# --------------------------------------------------------------------------- #
# AWS -- Pricing API. Must call the us-east-1 pricing endpoint regardless of
# which region is being priced (an AWS quirk -- `regionCode` is a filter
# value, not the endpoint region).
# --------------------------------------------------------------------------- #

async def fetch_aws_ec2_hourly_price(region: str, instance_type: str, env: Optional[dict] = None) -> Optional[float]:
    import json

    cmd = [
        "aws", "pricing", "get-products",
        "--service-code", "AmazonEC2",
        "--region", "us-east-1",  # Pricing API is only served from us-east-1
        "--filters",
        f"Type=TERM_MATCH,Field=instanceType,Value={instance_type}",
        f"Type=TERM_MATCH,Field=regionCode,Value={region}",
        "Type=TERM_MATCH,Field=operatingSystem,Value=Linux",
        "Type=TERM_MATCH,Field=tenancy,Value=Shared",
        "Type=TERM_MATCH,Field=preInstalledSw,Value=NA",
        "Type=TERM_MATCH,Field=capacitystatus,Value=Used",
        "--output", "json",
    ]
    try:
        _, stdout, _ = await run_cli(cmd, env=env)
    except Exception as exc:
        logger.info("AWS pricing lookup failed for %s/%s: %s", region, instance_type, exc)
        return None

    try:
        price_list = json.loads(stdout).get("PriceList", [])
        if not price_list:
            return None
        # Each entry is itself a JSON string (AWS's price list format).
        product = json.loads(price_list[0])
        on_demand = product["terms"]["OnDemand"]
        first_term = next(iter(on_demand.values()))
        first_dimension = next(iter(first_term["priceDimensions"].values()))
        return float(first_dimension["pricePerUnit"]["USD"])
    except (KeyError, StopIteration, ValueError, IndexError) as exc:
        logger.info("AWS pricing response for %s/%s had unexpected shape: %s", region, instance_type, exc)
        return None


# --------------------------------------------------------------------------- #
# GCP -- Cloud Billing Catalog API. Predefined machine types are billed as
# separate per-vCPU and per-GB-RAM SKUs rather than one bundled price.
# --------------------------------------------------------------------------- #

_COMPUTE_ENGINE_SERVICE = "services/6F81-5844-456A"

# (vCPUs per unit, GB RAM per vCPU) for machine_type patterns this supports.
# Deliberately narrow: these are the predefined, non-shared-core families
# that make up the vast majority of real GKE node pools. Shared-core
# (e2-micro/small/medium) and anything else falls through to "unsupported"
# rather than guessing at a ratio that doesn't apply.
_GCP_FAMILY_SHAPE = {
    "e2-standard": 4.0, "e2-highmem": 8.0, "e2-highcpu": 1.0,
    "n1-standard": 3.75, "n1-highmem": 6.5, "n1-highcpu": 0.9,
    "n2-standard": 4.0, "n2-highmem": 8.0, "n2-highcpu": 1.0,
    "n2d-standard": 4.0, "n2d-highmem": 8.0, "n2d-highcpu": 1.0,
}
# SKU description prefix used by each family in the Billing Catalog.
_GCP_FAMILY_SKU_PREFIX = {
    "e2-standard": "E2 Instance", "e2-highmem": "E2 Instance", "e2-highcpu": "E2 Instance",
    "n1-standard": "N1 Predefined Instance", "n1-highmem": "N1 Predefined Instance", "n1-highcpu": "N1 Predefined Instance",
    "n2-standard": "N2 Instance", "n2-highmem": "N2 Instance", "n2-highcpu": "N2 Instance",
    "n2d-standard": "N2D AMD Instance", "n2d-highmem": "N2D AMD Instance", "n2d-highcpu": "N2D AMD Instance",
}

_MACHINE_TYPE_RE = re.compile(r"^([a-z0-9]+-(?:standard|highmem|highcpu))-(\d+)$")


def _parse_gcp_machine_shape(machine_type: str) -> Optional[tuple[str, int, float]]:
    m = _MACHINE_TYPE_RE.match(machine_type.strip())
    if not m:
        return None
    family, vcpus_str = m.group(1), m.group(2)
    gb_per_vcpu = _GCP_FAMILY_SHAPE.get(family)
    if gb_per_vcpu is None:
        return None
    return family, int(vcpus_str), gb_per_vcpu


_sku_catalog_cache: dict = {"skus": None, "fetched_at": 0.0}
_SKU_CATALOG_TTL_SECONDS = 6 * 3600  # public list pricing changes rarely


async def _get_all_compute_skus(access_token: str) -> list:
    """Cloud Billing Catalog has ~30k+ Compute Engine SKUs with no
    server-side filter by description/region, so pricing one machine type
    means fetching the whole paginated list and filtering client-side.
    Cached process-wide (not per-region/project -- it's one universal public
    catalog) so pricing several node pools in one get_cost_estimate() call,
    or repeated calls within the TTL, don't each re-fetch ~33k SKUs."""
    import time

    if _sku_catalog_cache["skus"] is not None and (time.time() - _sku_catalog_cache["fetched_at"]) < _SKU_CATALOG_TTL_SECONDS:
        return _sku_catalog_cache["skus"]

    headers = {"Authorization": f"Bearer {access_token}"}
    skus = []
    page_token = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(10):  # hard cap: ~10*5000 = 50k SKUs, well beyond the real catalog size
            params = {"pageSize": 5000}
            if page_token:
                params["pageToken"] = page_token
            resp = await client.get(
                f"https://cloudbilling.googleapis.com/v1/{_COMPUTE_ENGINE_SERVICE}/skus",
                headers=headers, params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            skus.extend(data.get("skus", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    _sku_catalog_cache["skus"] = skus
    _sku_catalog_cache["fetched_at"] = time.time()
    return skus


async def _gcp_sku_unit_price(access_token: str, description_prefix: str, region: str) -> Optional[float]:
    skus = await _get_all_compute_skus(access_token)
    for sku in skus:
        desc = sku.get("description", "")
        category = sku.get("category", {})
        # Match on the description prefix alone (e.g. "N1 Predefined Instance
        # Core" vs "...Ram") -- resourceGroup isn't a reliable Core/Ram
        # discriminator across families (E2/N2/N2D use generic "CPU"/"RAM",
        # but N1 uses "N1Standard" for *both*), while the description prefix
        # also already excludes unrelated programs like "DWS Defined
        # Duration N1 Predefined..." or "Sole Tenancy N1...", which don't
        # start with the plain family prefix.
        if (
            category.get("usageType") == "OnDemand"
            and desc.startswith(description_prefix)
            and region in (sku.get("serviceRegions") or [])
        ):
            try:
                rate = sku["pricingInfo"][0]["pricingExpression"]["tieredRates"][-1]["unitPrice"]
                return int(rate.get("units", 0)) + rate.get("nanos", 0) / 1e9
            except (KeyError, IndexError):
                continue
    return None


async def fetch_gcp_compute_hourly_price(region: str, machine_type: str, access_token: str) -> Optional[float]:
    shape = _parse_gcp_machine_shape(machine_type)
    if shape is None:
        return None
    family, vcpus, gb_per_vcpu = shape
    sku_prefix = _GCP_FAMILY_SKU_PREFIX[family]

    try:
        cpu_rate = await _gcp_sku_unit_price(access_token, f"{sku_prefix} Core", region)
        ram_rate = await _gcp_sku_unit_price(access_token, f"{sku_prefix} Ram", region)
    except Exception as exc:
        logger.info("GCP billing catalog lookup failed for %s/%s: %s", region, machine_type, exc)
        return None

    if cpu_rate is None or ram_rate is None:
        return None
    return vcpus * cpu_rate + (vcpus * gb_per_vcpu) * ram_rate
