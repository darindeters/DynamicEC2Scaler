import boto3
import datetime
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import islice
from botocore.exceptions import ClientError

session = boto3.session.Session()
s3 = session.client("s3")
cloudwatch = session.client("cloudwatch")
cost_explorer = session.client("ce", region_name="us-east-1")
_thread_local = threading.local()
PRICE_CACHE = {}
PRICE_CACHE_LOCK = threading.Lock()
SAVINGS_PLAN_FACTOR = None
SAVINGS_PLAN_FACTOR_SOURCE = None
SAVINGS_PLAN_LOCK = threading.Lock()
DEFAULT_METRIC_NAMESPACE = "DynamicEC2Scaler/Savings"
VALID_ACTIONS = {"scaleup", "scaledown"}
RUN_CONTEXT = {}
LAST_SCALE_DOWN_TIMESTAMP_TAG = "DynamicScalingLastScaleDownTimestamp"
LAST_SCALE_DOWN_HOURLY_TAG = "DynamicScalingLastScaleDownHourlySavings"
LAST_SCALE_UP_TIMESTAMP_TAG = "DynamicScalingLastScaleUpTimestamp"
SCALE_UP_CRON_ENV = "SCALE_UP_CRON_EXPRESSION"
DEFAULT_SCHEDULE_NAME = "default"
SCHEDULE_ALL_TOKEN = "all"

REGION_NAME_MAP = {
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-south-2": "Asia Pacific (Hyderabad)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-southeast-4": "Asia Pacific (Melbourne)",
    "ca-central-1": "Canada (Central)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "EU (Milan)",
    "eu-south-2": "EU (Spain)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "me-south-1": "Middle East (Bahrain)",
    "sa-east-1": "South America (São Paulo)",
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
}

CRON_WEEKDAY_MAP = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}

def parse_int_env(var_name, default, minimum=None, maximum=None):
    raw_value = os.getenv(var_name)
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value

def parse_float_env(var_name, default, minimum=None):
    raw_value = os.getenv(var_name)
    try:
        value = float(str(raw_value).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        value = minimum
    return value

def parse_bool_env(var_name, default=False):
    raw_value = os.getenv(var_name)
    if raw_value is None:
        return default
    return str(raw_value).strip().lower() == "true"

def chunks(iterable, size):
    iterator = iter(iterable)
    while True:
        batch = list(islice(iterator, size))
        if not batch:
            break
        yield batch

BATCH_SIZE = parse_int_env("BATCH_SIZE", 10, 1)
MAX_RETRIES = parse_int_env("MAX_RETRIES", 3, 1)
BACKOFF_SECS = parse_float_env("BACKOFF_SECS", 5.0, 0.0)
FAIL_FAST = parse_bool_env("FAIL_FAST", False)
DEFAULT_DOWNSIZE_TYPE = (
    os.getenv("DEFAULT_DOWNSIZE_TYPE", "t3.medium").strip() or "t3.medium"
)

def get_ec2_client():
    client = getattr(_thread_local, "ec2", None)
    if client is None:
        client = session.client("ec2")
        _thread_local.ec2 = client
    return client

def get_pricing_client():
    client = getattr(_thread_local, "pricing", None)
    if client is None:
        client = session.client("pricing", region_name="us-east-1")
        _thread_local.pricing = client
    return client

def retry(api_call, *args, **kwargs):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return api_call(*args, **kwargs)
        except ClientError as exc:
            last_error = exc
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            print(f"Retry {attempt}/{MAX_RETRIES} for {api_call.__name__}: {code}")
        except Exception as exc:
            last_error = exc
            print(f"Retry {attempt}/{MAX_RETRIES} for {api_call.__name__}: {exc}")
        time.sleep(BACKOFF_SECS * attempt)

    if last_error:
        raise last_error
    raise RuntimeError("Retry failed without capturing an error")

def get_schedule_tag_key():
    raw_key = os.environ.get("SCHEDULE_TAG_KEY", "DynamicScalingSchedule")
    key = raw_key.strip()
    return key or "DynamicScalingSchedule"

def get_tag_value(tags, key):
    if key in tags:
        return tags[key]
    for tag_key, value in tags.items():
        if tag_key.lower() == key.lower():
            return value
    return None

def normalize_schedule_name(value):
    if value is None:
        return DEFAULT_SCHEDULE_NAME
    normalized = value.strip().lower()
    if not normalized:
        return DEFAULT_SCHEDULE_NAME
    return normalized

def parse_schedule_tag_value(raw_value):
    if not raw_value:
        return []
    values = []
    for token in raw_value.split(","):
        cleaned = token.strip().lower()
        if cleaned:
            values.append(cleaned)
    return values

def instance_matches_schedule(tags, schedule_name):
    if schedule_name == SCHEDULE_ALL_TOKEN:
        return True
    schedule_tag_key = get_schedule_tag_key()
    raw_value = get_tag_value(tags, schedule_tag_key)
    if not raw_value:
        return schedule_name == DEFAULT_SCHEDULE_NAME
    values = parse_schedule_tag_value(raw_value)
    if not values:
        return schedule_name == DEFAULT_SCHEDULE_NAME
    if SCHEDULE_ALL_TOKEN in values:
        return True
    return schedule_name in values

def get_concurrency_limit():
    raw_value = os.environ.get("MAX_CONCURRENT_OPERATIONS")
    if raw_value is None:
        return 4
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return 4
    if value < 1:
        return 1
    if value > 20:
        return 20
    return value

def wait_for_instance_type(client, instance_id, desired_type, timeout_seconds=300, poll_interval=5):
    print(f"Waiting for {instance_id} instance type to update to {desired_type}...")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = retry(client.describe_instances, InstanceIds=[instance_id])
        reservations = response.get("Reservations", [])
        for reservation in reservations:
            for inst in reservation.get("Instances", []):
                current_type = inst.get("InstanceType")
                if current_type == desired_type:
                    print(f"{instance_id} instance type confirmed as {desired_type}.")
                    return
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, max(remaining, 1)))
    raise TimeoutError(
        f"Instance {instance_id} did not report instance type {desired_type} within {timeout_seconds} seconds."
    )

def format_utc(dt):
    if not isinstance(dt, datetime.datetime):
        raise TypeError("Expected datetime instance when formatting timestamp")
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0).isoformat() + "Z"

def set_run_start_time(dt):
    RUN_CONTEXT["start_time"] = dt

def get_run_start_time():
    return RUN_CONTEXT.get("start_time")

def parse_timestamp(value):
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return parsed

def cron_value_to_weekday(token):
    upper_token = token.strip().upper()
    if not upper_token:
        raise ValueError("Empty day-of-week token in cron expression")
    if upper_token in CRON_WEEKDAY_MAP:
        return CRON_WEEKDAY_MAP[upper_token]
    try:
        numeric = int(token)
    except ValueError as exc:
        raise ValueError(f"Unsupported day-of-week token: {token}") from exc
    if numeric in (0, 7):
        return 6
    if 1 <= numeric <= 6:
        return numeric - 1
    raise ValueError(f"Day-of-week value out of range: {token}")

def parse_day_of_week_field(field):
    normalized = (field or "*").strip().upper()
    if normalized in {"*", "?"}:
        return None
    allowed = set()
    for segment in normalized.split(","):
        segment = segment.strip()
        if not segment:
            continue
        if "-" in segment:
            start_token, end_token = segment.split("-", 1)
            start = cron_value_to_weekday(start_token)
            end = cron_value_to_weekday(end_token)
            if start <= end:
                for value in range(start, end + 1):
                    allowed.add(value)
            else:
                for value in list(range(start, 7)) + list(range(0, end + 1)):
                    allowed.add(value)
        else:
            allowed.add(cron_value_to_weekday(segment))
    return allowed

def parse_single_int_field(value, field_name, minimum, maximum):
    cleaned = (value or "").strip()
    if cleaned in {"*", "?"}:
        raise ValueError(
            f"{field_name} field must be a fixed numeric value in cron expression"
        )
    try:
        numeric = int(cleaned)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} field must be an integer in cron expression"
        ) from exc
    if numeric < minimum or numeric > maximum:
        raise ValueError(
            f"{field_name} field value {numeric} is out of the allowed range {minimum}-{maximum}"
        )
    return numeric

def parse_cron_expression(cron_expression):
    if not cron_expression:
        raise ValueError("Cron expression is empty")
    expression = cron_expression.strip()
    if expression.lower().startswith("cron(") and expression.endswith(")"):
        expression = expression[5:-1].strip()
    parts = expression.split()
    if len(parts) != 6:
        raise ValueError(f"Cron expression must have 6 fields: {cron_expression}")
    (
        minute_field,
        hour_field,
        day_of_month_field,
        month_field,
        day_of_week_field,
        year_field,
    ) = parts
    minute = parse_single_int_field(minute_field, "minute", 0, 59)
    hour = parse_single_int_field(hour_field, "hour", 0, 23)
    if day_of_month_field not in {"*", "?"}:
        raise ValueError(
            "Day-of-month field must be '*' or '?' for supported cron expressions"
        )
    if month_field not in {"*"}:
        raise ValueError("Month field must be '*' for supported cron expressions")
    if year_field not in {"*"}:
        raise ValueError("Year field must be '*' for supported cron expressions")
    allowed_days = parse_day_of_week_field(day_of_week_field)
    return minute, hour, allowed_days

def get_next_scheduled_time(cron_expression, reference_time):
    minute, hour, allowed_days = parse_cron_expression(cron_expression)
    baseline = reference_time.replace(second=0, microsecond=0)
    for day_offset in range(0, 14):
        candidate_date = baseline.date() + datetime.timedelta(days=day_offset)
        candidate = datetime.datetime.combine(
            candidate_date,
            datetime.time(hour=hour, minute=minute),
        )
        if candidate <= reference_time:
            continue
        if allowed_days is not None and candidate.weekday() not in allowed_days:
            continue
        return candidate
    raise ValueError(
        "Unable to find the next scheduled time within 14 days for cron expression: "
        f"{cron_expression}"
    )

def compute_projected_savings(total_hourly_savings, reference_time):
    schedule_expression = os.environ.get(SCALE_UP_CRON_ENV)
    if not schedule_expression:
        raise ValueError("Scale up cron expression environment variable is not set")
    next_scale_up = get_next_scheduled_time(schedule_expression, reference_time)
    duration_hours = max((next_scale_up - reference_time).total_seconds() / 3600.0, 0.0)
    projected_total = round(total_hourly_savings * duration_hours, 4)
    return {
        "projection_source": "scale_up_schedule",
        "projected_scale_up_time_utc": format_utc(next_scale_up),
        "projected_off_hours_duration_hours": round(duration_hours, 4),
        "projected_total_savings": projected_total,
    }

DEFAULT_PRICING_FILTERS = {
    "operatingSystem": os.environ.get("DEFAULT_PRICING_OPERATING_SYSTEM", "Linux"),
    "preInstalledSw": os.environ.get("DEFAULT_PRICING_PREINSTALLED_SOFTWARE", "NA"),
    "licenseModel": os.environ.get("DEFAULT_PRICING_LICENSE_MODEL", "No License required"),
}

PLATFORM_FILTER_RULES = [
    (
        "windows with sql server enterprise",
        {
            "operatingSystem": "Windows",
            "preInstalledSw": "SQL Server Enterprise",
            "licenseModel": "License Included",
        },
    ),
    (
        "windows with sql server standard",
        {
            "operatingSystem": "Windows",
            "preInstalledSw": "SQL Server Standard",
            "licenseModel": "License Included",
        },
    ),
    (
        "windows with sql server web",
        {
            "operatingSystem": "Windows",
            "preInstalledSw": "SQL Server Web",
            "licenseModel": "License Included",
        },
    ),
    (
        "windows",
        {
            "operatingSystem": "Windows",
            "preInstalledSw": "NA",
            "licenseModel": "License Included",
        },
    ),
    (
        "bring your own license",
        {
            "operatingSystem": "Windows",
            "preInstalledSw": "NA",
            "licenseModel": "Bring your own license",
        },
    ),
    (
        "byol",
        {
            "operatingSystem": "Windows",
            "preInstalledSw": "NA",
            "licenseModel": "Bring your own license",
        },
    ),
    (
        "red hat enterprise linux",
        {
            "operatingSystem": "RHEL",
            "preInstalledSw": "NA",
            "licenseModel": "No License required",
        },
    ),
    (
        "rhel",
        {
            "operatingSystem": "RHEL",
            "preInstalledSw": "NA",
            "licenseModel": "No License required",
        },
    ),
    (
        "suse",
        {
            "operatingSystem": "SUSE",
            "preInstalledSw": "NA",
            "licenseModel": "No License required",
        },
    ),
    (
        "linux",
        {
            "operatingSystem": "Linux",
            "preInstalledSw": "NA",
            "licenseModel": "No License required",
        },
    ),
]

def get_region():
    region_name = (
        session.region_name
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    )
    if not region_name:
        print(
            "AWS region could not be determined for pricing lookup. "
            "Defaulting to 'unknown'."
        )
        return "unknown"
    return region_name

def get_location(region_name):
    location = REGION_NAME_MAP.get(region_name)
    if not location:
        print(
            "Unsupported region for pricing lookup: "
            f"{region_name}. Defaulting location to 'Unknown'."
        )
        return "Unknown"
    return location

def build_pricing_cache_key(instance_type, pricing_filters):
    return (
        instance_type,
        pricing_filters.get("operatingSystem"),
        pricing_filters.get("preInstalledSw"),
        pricing_filters.get("licenseModel"),
    )

def get_instance_pricing_profile(instance):
    platform_candidates = [
        instance.get("PlatformDetails"),
        instance.get("Platform"),
        instance.get("UsageOperation"),
    ]

    for platform_text in platform_candidates:
        normalized = (platform_text or "").strip().lower()
        if not normalized:
            continue
        for match, filters in PLATFORM_FILTER_RULES:
            if match in normalized:
                return dict(filters), f"platform:{platform_text}"

    return dict(DEFAULT_PRICING_FILTERS), "default"

def get_hourly_rate(instance_type, pricing_filters):
    cache_key = build_pricing_cache_key(instance_type, pricing_filters)
    with PRICE_CACHE_LOCK:
        cached_price = PRICE_CACHE.get(cache_key)
    if cached_price is not None:
        return cached_price

    region_name = get_region()
    location = get_location(region_name)
    if region_name == "unknown" or location == "Unknown":
        print(
            "Pricing lookup skipped because region or location could not be determined. "
            "Defaulting hourly rate to $0.0."
        )
        return 0.0
    pricing_client = get_pricing_client()

    def build_filters():
        attribute_filters = []
        for field in ("operatingSystem", "preInstalledSw", "licenseModel"):
            value = pricing_filters.get(field)
            if value:
                attribute_filters.append(
                    {"Type": "TERM_MATCH", "Field": field, "Value": value}
                )

        base_filters = [
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
        ]
        base_filters.extend(attribute_filters)
        base_filters.extend(
            [
                {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
            ]
        )
        return base_filters

    def remove_filter(filters, field_name):
        return [
            filter_item
            for filter_item in filters
            if filter_item.get("Field") != field_name
        ]

    def update_filter(filters, field_name, value):
        updated = []
        for filter_item in filters:
            if filter_item.get("Field") == field_name:
                updated.append({**filter_item, "Value": value})
            else:
                updated.append(dict(filter_item))
        return updated

    def serialize_filters(filters):
        return tuple(
            sorted(
                (item.get("Field"), item.get("Value"))
                for item in filters
            )
        )

    def format_filters(filters):
        parts = []
        for item in filters:
            field = item.get("Field")
            value = item.get("Value")
            if field and value:
                parts.append(f"{field}={value}")
        return ", ".join(parts)

    base_filters = build_filters()
    filter_variants = [base_filters]

    license_value = pricing_filters.get("licenseModel")
    if license_value:
        if license_value == "License Included":
            filter_variants.append(
                update_filter(base_filters, "licenseModel", "No License required")
            )
        filter_variants.append(remove_filter(base_filters, "licenseModel"))

    preinstalled_value = pricing_filters.get("preInstalledSw")
    if preinstalled_value:
        filter_variants.append(remove_filter(base_filters, "preInstalledSw"))

    if license_value and preinstalled_value:
        filter_variants.append(
            remove_filter(
                remove_filter(base_filters, "licenseModel"),
                "preInstalledSw",
            )
        )

    unique_variants = []
    seen_variants = set()
    for candidate in filter_variants:
        signature = serialize_filters(candidate)
        if signature not in seen_variants:
            seen_variants.add(signature)
            unique_variants.append(candidate)

    attempted_filters = []

    def try_filters(filters_to_use):
        response = pricing_client.get_products(
            ServiceCode="AmazonEC2",
            Filters=filters_to_use,
            MaxResults=1,
        )
        price_list = response.get("PriceList")
        attempted_filters.append(format_filters(filters_to_use))
        if not price_list:
            return None

        price_item = json.loads(price_list[0])
        on_demand_terms = price_item["terms"].get("OnDemand", {})
        for term in on_demand_terms.values():
            price_dimensions = term.get("priceDimensions", {})
            for dimension in price_dimensions.values():
                return float(dimension["pricePerUnit"].get("USD", "0"))
        return None

    for filters_to_use in unique_variants:
        price = try_filters(filters_to_use)
        if price is not None:
            with PRICE_CACHE_LOCK:
                PRICE_CACHE[cache_key] = price
            return price

    attempted_summary = "; ".join(attempted_filters) or "(none)"
    raise ValueError(
        "No pricing information found for "
        f"{instance_type} ({pricing_filters}) in {region_name} "
        f"using filters: {attempted_summary}"
    )

def parse_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def get_manual_discount_factor():
    raw_value = os.environ.get("SAVINGS_PLAN_DISCOUNT_PERCENT", "0")
    try:
        discount_percent = float(raw_value)
    except ValueError as exc:
        raise ValueError(
            "SAVINGS_PLAN_DISCOUNT_PERCENT must be a number between 0 and 100"
        ) from exc

    if discount_percent < 0 or discount_percent > 100:
        raise ValueError(
            "SAVINGS_PLAN_DISCOUNT_PERCENT must be between 0 and 100"
        )

    return 1 - (discount_percent / 100.0)

def get_coverage_discount_factor():
    raw_lookback = os.environ.get("SAVINGS_PLAN_COVERAGE_LOOKBACK_DAYS", "30")
    try:
        lookback_days = int(raw_lookback)
    except ValueError as exc:
        raise ValueError(
            "SAVINGS_PLAN_COVERAGE_LOOKBACK_DAYS must be an integer between 1 and 90"
        ) from exc

    if lookback_days < 1 or lookback_days > 90:
        raise ValueError(
            "SAVINGS_PLAN_COVERAGE_LOOKBACK_DAYS must be between 1 and 90"
        )

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=lookback_days)

    total_savings = 0.0
    total_cost = 0.0
    next_token = None

    while True:
        kwargs = {
            "TimePeriod": {
                "Start": start_date.isoformat(),
                "End": end_date.isoformat(),
            },
            "Granularity": "DAILY",
        }
        if next_token:
            kwargs["NextToken"] = next_token

        response = cost_explorer.get_savings_plans_coverage(**kwargs)
        for coverage in response.get("SavingsPlansCoverages", []):
            data = coverage.get("Coverage", {})
            total_savings += parse_float(data.get("SavingsPlansSavings"))
            total_cost += parse_float(data.get("TotalCost"))

        next_token = response.get("NextToken")
        if not next_token:
            break

    denominator = total_cost + total_savings
    if denominator <= 0:
        raise ValueError("Savings Plan coverage data is empty for the selected window")

    discount_percent = max(min(total_savings / denominator, 0.9999), 0.0) * 100
    print(
        "Derived Savings Plan discount percent from Cost Explorer coverage: "
        f"{discount_percent:.4f}% over the last {lookback_days} days"
    )
    return 1 - (discount_percent / 100.0)

def get_savings_plan_factor():
    global SAVINGS_PLAN_FACTOR
    global SAVINGS_PLAN_FACTOR_SOURCE
    with SAVINGS_PLAN_LOCK:
        if SAVINGS_PLAN_FACTOR is not None:
            return SAVINGS_PLAN_FACTOR

        mode = os.environ.get("SAVINGS_PLAN_DISCOUNT_MODE", "Manual").strip().lower()

        if mode == "coverage":
            try:
                SAVINGS_PLAN_FACTOR = get_coverage_discount_factor()
                SAVINGS_PLAN_FACTOR_SOURCE = "coverage"
                return SAVINGS_PLAN_FACTOR
            except Exception as coverage_error:
                print(
                    "Falling back to manual Savings Plan discount due to coverage error: "
                    f"{coverage_error}"
                )

        SAVINGS_PLAN_FACTOR = get_manual_discount_factor()
        SAVINGS_PLAN_FACTOR_SOURCE = "manual"
        return SAVINGS_PLAN_FACTOR

def get_metric_namespace():
    raw_value = os.environ.get("SAVINGS_METRIC_NAMESPACE")
    if raw_value is None:
        return DEFAULT_METRIC_NAMESPACE
    namespace = raw_value.strip()
    return namespace

def get_downsize_type():
    return DEFAULT_DOWNSIZE_TYPE

def publish_savings_metrics(report, timestamp):
    namespace = get_metric_namespace()
    if not namespace:
        print("Savings metric namespace not configured. Skipping CloudWatch metrics.")
        return

    metric_data = [
        {
            "MetricName": "TotalHourlySavings",
            "Dimensions": [
                {"Name": "Region", "Value": report["region"]},
            ],
            "Timestamp": timestamp,
            "Value": report["total_hourly_savings"],
            "Unit": "None",
        }
    ]

    projected_total = report.get("projected_total_savings")
    if projected_total is not None:
        metric_data.append(
            {
                "MetricName": "TotalProjectedOffHoursSavings",
                "Dimensions": [
                    {"Name": "Region", "Value": report["region"]},
                ],
                "Timestamp": timestamp,
                "Value": projected_total,
                "Unit": "None",
            }
        )

    projected_duration = report.get("projected_off_hours_duration_hours")
    if projected_duration is not None:
        metric_data.append(
            {
                "MetricName": "ProjectedOffHoursDurationHours",
                "Dimensions": [
                    {"Name": "Region", "Value": report["region"]},
                ],
                "Timestamp": timestamp,
                "Value": projected_duration,
                "Unit": "None",
            }
        )

    for instance in report["instances"]:
        metric_data.append(
            {
                "MetricName": "InstanceHourlySavings",
                "Dimensions": [
                    {"Name": "Region", "Value": report["region"]},
                    {"Name": "InstanceId", "Value": instance["instance_id"]},
                ],
                "Timestamp": timestamp,
                "Value": instance["hourly_savings"],
                "Unit": "None",
            }
        )

    for i in range(0, len(metric_data), 20):
        batch = metric_data[i : i + 20]
        try:
            cloudwatch.put_metric_data(Namespace=namespace, MetricData=batch)
        except ClientError as metric_error:
            print(
                "Failed to publish savings metrics batch to CloudWatch: "
                f"{metric_error}"
            )
        except Exception as metric_error:
            print(
                "Unexpected error while publishing savings metrics batch: "
                f"{metric_error}"
            )

def build_actual_savings_snapshot(instance_id, tags, desired_type, current_type):
    last_down_value = tags.get(LAST_SCALE_DOWN_TIMESTAMP_TAG)
    hourly_value = tags.get(LAST_SCALE_DOWN_HOURLY_TAG)

    if not last_down_value or not hourly_value:
        print(
            "Missing scale-down metadata for "
            f"{instance_id}. Skipping actual savings calculation."
        )
        return None

    scale_down_time = parse_timestamp(last_down_value)
    if not scale_down_time:
        print(
            "Unable to parse scale-down timestamp for "
            f"{instance_id}: {last_down_value}"
        )
        return None

    last_scale_up_value = tags.get(LAST_SCALE_UP_TIMESTAMP_TAG)
    last_scale_up_time = parse_timestamp(last_scale_up_value)
    if last_scale_up_time and last_scale_up_time >= scale_down_time:
        print(
            "Actual savings already recorded after the last scale-down "
            f"window for {instance_id}. Skipping."
        )
        return None

    try:
        hourly_savings = float(hourly_value)
    except ValueError:
        print(
            "Invalid hourly savings tag for "
            f"{instance_id}: {hourly_value}"
        )
        return None

    scale_up_time = datetime.datetime.utcnow().replace(microsecond=0)
    downtime_hours = max(
        (scale_up_time - scale_down_time).total_seconds() / 3600.0,
        0.0,
    )
    actual_savings_value = round(hourly_savings * downtime_hours, 4)

    print(
        f"Measured downtime for {instance_id}: {downtime_hours:.4f} hours, "
        f"actual savings ${actual_savings_value:.4f}"
    )

    record = {
        "instance_id": instance_id,
        "off_hours_type": current_type,
        "restored_type": desired_type,
        "scale_down_timestamp": format_utc(scale_down_time),
        "scale_up_timestamp": format_utc(scale_up_time),
        "downtime_hours": round(downtime_hours, 4),
        "hourly_savings": round(hourly_savings, 4),
        "actual_savings": actual_savings_value,
        "actual_savings_source": f"tag:{LAST_SCALE_DOWN_HOURLY_TAG}",
    }

    return record, scale_up_time

def publish_actual_savings_metrics(report, timestamp):
    namespace = get_metric_namespace()
    if not namespace:
        print("Savings metric namespace not configured. Skipping CloudWatch metrics.")
        return

    metric_data = [
        {
            "MetricName": "TotalActualSavings",
            "Dimensions": [
                {"Name": "Region", "Value": report["region"]},
            ],
            "Timestamp": timestamp,
            "Value": report["total_actual_savings"],
            "Unit": "None",
        },
        {
            "MetricName": "TotalActualDowntimeHours",
            "Dimensions": [
                {"Name": "Region", "Value": report["region"]},
            ],
            "Timestamp": timestamp,
            "Value": report["total_actual_downtime_hours"],
            "Unit": "None",
        },
    ]

    hourly_basis = report.get("total_hourly_savings_basis")
    if hourly_basis is not None:
        metric_data.append(
            {
                "MetricName": "TotalActualHourlySavingsBasis",
                "Dimensions": [
                    {"Name": "Region", "Value": report["region"]},
                ],
                "Timestamp": timestamp,
                "Value": hourly_basis,
                "Unit": "None",
            }
        )

    for instance in report.get("instances", []):
        instance_id = instance.get("instance_id")
        if not instance_id:
            continue
        actual_savings = instance.get("actual_savings")
        if actual_savings is not None:
            metric_data.append(
                {
                    "MetricName": "InstanceActualSavings",
                    "Dimensions": [
                        {"Name": "Region", "Value": report["region"]},
                        {"Name": "InstanceId", "Value": instance_id},
                    ],
                    "Timestamp": timestamp,
                    "Value": actual_savings,
                    "Unit": "None",
                }
            )
        downtime_value = instance.get("downtime_hours")
        if downtime_value is not None:
            metric_data.append(
                {
                    "MetricName": "InstanceDowntimeHours",
                    "Dimensions": [
                        {"Name": "Region", "Value": report["region"]},
                        {"Name": "InstanceId", "Value": instance_id},
                    ],
                    "Timestamp": timestamp,
                    "Value": downtime_value,
                    "Unit": "None",
                }
            )

    for i in range(0, len(metric_data), 20):
        batch = metric_data[i : i + 20]
        try:
            cloudwatch.put_metric_data(Namespace=namespace, MetricData=batch)
        except ClientError as metric_error:
            print(
                "Failed to publish actual savings metrics batch to CloudWatch: "
                f"{metric_error}"
            )
        except Exception as metric_error:
            print(
                "Unexpected error while publishing actual savings metrics batch: "
                f"{metric_error}"
            )

def get_run_metadata():
    run_time = get_run_start_time() or datetime.datetime.utcnow()
    run_time = run_time.replace(microsecond=0)
    return run_time, format_utc(run_time), get_region()

def write_savings_report(prefix, summary, run_time):
    bucket = os.environ.get("SAVINGS_BUCKET")
    if not bucket:
        print(
            "SAVINGS_BUCKET environment variable is not set; skipping "
            f"{prefix} report write."
        )
        return None

    timestamp = summary.get("timestamp") or format_utc(run_time)
    key = f"{prefix}/{run_time.date()}/{timestamp}.json"

    print(f"Writing {prefix} report to s3://{bucket}/{key}")
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(summary, indent=2).encode("utf-8"),
        )
    except ClientError as s3_error:
        print(
            "Failed to write savings report to S3: "
            f"{s3_error}"
        )
    except Exception as s3_error:
        print(
            "Unexpected error while writing savings report to S3: "
            f"{s3_error}"
        )

def record_savings(savings_records):
    run_time, timestamp, region = get_run_metadata()
    savings_plan_factor = get_savings_plan_factor()
    total_hourly_savings = round(
        sum(r["hourly_savings"] for r in savings_records), 4
    ) if savings_records else 0.0

    summary = {
        "timestamp": timestamp,
        "region": region,
        "savings_plan_discount_percent": round(
            (1 - savings_plan_factor) * 100, 4
        ),
        "savings_plan_discount_source": SAVINGS_PLAN_FACTOR_SOURCE,
        "total_hourly_savings": total_hourly_savings,
        "instances": savings_records,
    }

    try:
        projection_details = compute_projected_savings(
            total_hourly_savings,
            run_time,
        )
    except Exception as projection_error:
        print(
            "Unable to derive projected savings window: "
            f"{projection_error}"
        )
        summary["projection_error"] = str(projection_error)
    else:
        summary.update(projection_details)

    if not savings_records:
        summary["zero_savings"] = True
        summary["zero_savings_reason"] = "no_savings_records_generated"
        summary["zero_savings_flags"] = ["no_savings_records"]

    log_event = {
        "level": "INFO",
        "event": "SavingsRunSummary",
        "summary": summary,
    }
    print(json.dumps(log_event))

    write_savings_report("savings", summary, run_time)

    if savings_records:
        publish_savings_metrics(summary, run_time)

def record_actual_savings(actual_records):
    run_time, timestamp, region = get_run_metadata()

    total_actual_savings = round(
        sum(record.get("actual_savings", 0.0) for record in actual_records),
        4,
    ) if actual_records else 0.0
    total_downtime_hours = round(
        sum(record.get("downtime_hours", 0.0) for record in actual_records),
        4,
    ) if actual_records else 0.0
    total_hourly_basis = round(
        sum(record.get("hourly_savings", 0.0) for record in actual_records),
        4,
    ) if actual_records else 0.0

    summary = {
        "timestamp": timestamp,
        "region": region,
        "calculation_source": "instance_tag_metadata",
        "total_actual_savings": total_actual_savings,
        "total_actual_downtime_hours": total_downtime_hours,
        "total_hourly_savings_basis": total_hourly_basis,
        "instances": actual_records,
    }

    if not actual_records:
        summary["zero_savings"] = True
        summary["zero_savings_reason"] = "no_actual_savings_records"
        summary["zero_savings_flags"] = ["no_actual_records"]

    log_event = {
        "level": "INFO",
        "event": "ActualSavingsSummary",
        "summary": summary,
    }
    print(json.dumps(log_event))

    write_savings_report("actual-savings", summary, run_time)

    if actual_records:
        publish_actual_savings_metrics(summary, run_time)

def process_instance(instance_data, action, run_start_timestamp, downsize_type=None):
    instance = instance_data["instance"]
    instance_id = instance_data["instance_id"]
    current_type = instance_data["current_type"]
    state = instance_data["state"]
    tags = dict(instance_data["tags"])
    ec2_client = get_ec2_client()

    savings_record = None
    actual_record = None
    actual_snapshot_params = None

    print(f"\nProcessing {instance_id} ({state}, {current_type})")

    if action == "scaledown":
        desired_type = downsize_type or get_downsize_type()
        if current_type == desired_type:
            print("Already at downsized type. Skipping.")
            return False, None, None

        preferred_type = (tags.get("PreferredInstanceType") or "").strip()
        if preferred_type:
            print(
                "PreferredInstanceType tag already set to "
                f"{preferred_type}. Preserving existing value and continuing with downsizing."
            )
        else:
            print(f"Tagging {instance_id} with PreferredInstanceType = {current_type}")
            retry(
                ec2_client.create_tags,
                Resources=[instance_id],
                Tags=[{"Key": "PreferredInstanceType", "Value": current_type}],
            )
            tags["PreferredInstanceType"] = current_type

        try:
            pricing_filters, pricing_source = get_instance_pricing_profile(instance)
            print(
                "Using pricing profile for "
                f"{instance_id}: {pricing_filters} (source={pricing_source})"
            )
            original_rate = get_hourly_rate(current_type, pricing_filters)
            downsized_rate = get_hourly_rate(desired_type, pricing_filters)
            hourly_savings = max(original_rate - downsized_rate, 0)
            hourly_savings *= get_savings_plan_factor()
            record = {
                "instance_id": instance_id,
                "previous_type": current_type,
                "downsized_type": desired_type,
                "pricing_operating_system": pricing_filters["operatingSystem"],
                "pricing_preinstalled_software": pricing_filters["preInstalledSw"],
                "pricing_license_model": pricing_filters["licenseModel"],
                "pricing_profile_source": pricing_source,
                "hourly_savings": round(hourly_savings, 4),
                "scale_down_timestamp": run_start_timestamp,
            }
            savings_record = record
            retry(
                ec2_client.create_tags,
                Resources=[instance_id],
                Tags=[
                    {
                        "Key": LAST_SCALE_DOWN_TIMESTAMP_TAG,
                        "Value": run_start_timestamp,
                    },
                    {
                        "Key": LAST_SCALE_DOWN_HOURLY_TAG,
                        "Value": f"{hourly_savings:.4f}",
                    },
                ],
            )
            print(
                "Recorded scale-down metadata for "
                f"{instance_id}: timestamp={run_start_timestamp}, "
                f"hourly_savings=${hourly_savings:.4f}"
            )
            print(
                f"Estimated hourly savings for {instance_id}: ${hourly_savings:.4f}"
            )
        except Exception as pricing_error:
            print(
                "Unable to calculate savings for "
                f"{instance_id}: {pricing_error}"
            )

        desired_type_value = desired_type

    else:
        desired_type_value = (tags.get("PreferredInstanceType") or "").strip()
        if not desired_type_value:
            print("No PreferredInstanceType tag found. Skipping.")
            return False, None, None
        if current_type == desired_type_value:
            print("Already at desired type. Skipping.")
            return False, None, None

        actual_snapshot_params = {
            "instance_id": instance_id,
            "tags": tags,
            "desired_type": desired_type_value,
            "current_type": current_type,
        }

    if state != "stopped":
        print(f"Stopping {instance_id}...")
        retry(ec2_client.stop_instances, InstanceIds=[instance_id])
        retry(
            ec2_client.get_waiter("instance_stopped").wait,
            InstanceIds=[instance_id],
        )
        print(f"{instance_id} stopped.")
    else:
        print(f"{instance_id} already stopped.")

    print(f"Modifying {instance_id} to {desired_type_value}...")
    retry(
        ec2_client.modify_instance_attribute,
        InstanceId=instance_id,
        InstanceType={"Value": desired_type_value},
    )
    wait_for_instance_type(ec2_client, instance_id, desired_type_value)

    print(f"Starting {instance_id}...")
    retry(ec2_client.start_instances, InstanceIds=[instance_id])
    retry(
        ec2_client.get_waiter("instance_running").wait,
        InstanceIds=[instance_id],
    )
    print(f"{instance_id} is running.")

    if action == "scaleup" and actual_snapshot_params is not None:
        actual_result = build_actual_savings_snapshot(
            **actual_snapshot_params
        )
        if actual_result:
            actual_record, actual_snapshot_time = actual_result
            scale_up_timestamp_value = format_utc(actual_snapshot_time)
            retry(
                ec2_client.create_tags,
                Resources=[instance_id],
                Tags=[
                    {
                        "Key": LAST_SCALE_UP_TIMESTAMP_TAG,
                        "Value": scale_up_timestamp_value,
                    }
                ],
            )
            print(
                f"Recorded scale-up metadata for {instance_id}: "
                f"timestamp={scale_up_timestamp_value}"
            )

    return True, savings_record, actual_record

def lambda_handler(event, _context):
    action = event.get("action")
    source = event.get("source", "manual")
    schedule_name = normalize_schedule_name(event.get("schedule"))

    if action not in VALID_ACTIONS:
        raise ValueError(f"Invalid or missing 'action'. Must be one of: {VALID_ACTIONS}")

    if source == "manual":
        raise Exception("Manual execution is blocked. Use EventBridge scheduled rules.")

    run_start_time = datetime.datetime.utcnow().replace(microsecond=0)
    set_run_start_time(run_start_time)
    run_start_timestamp = format_utc(run_start_time)

    print(
        f"Starting EC2 {action} process for schedule '{schedule_name}'..."
    )

    ec2_client = get_ec2_client()
    filters = [
        {"Name": "tag:DynamicInstanceScaling", "Values": ["true"]},
        {"Name": "instance-state-name", "Values": ["running", "stopped"]},
    ]

    reservations = []
    paginator = ec2_client.get_paginator("describe_instances")
    try:
        for page in paginator.paginate(Filters=filters):
            reservations.extend(page.get("Reservations", []))
    except ClientError as describe_error:
        print(
            "Unable to describe instances for scaling run: "
            f"{describe_error}"
        )
        return {
            "processed_instances": 0,
            "action": action,
            "schedule": schedule_name,
            "skipped_instances": 0,
            "error": "describe_instances_failed",
        }

    if not reservations:
        print(
            "No EC2 instances matched DynamicInstanceScaling=true in running/stopped states. "
            "Skipping stop/modify/start workflow and savings recording."
        )
        return {
            "processed_instances": 0,
            "action": action,
            "schedule": schedule_name,
            "skipped_instances": 0,
        }

    instances_to_process = []
    skipped_due_to_schedule = 0
    seen_instance_ids = set()

    for reservation in reservations:
        for instance in reservation.get("Instances", []):
            instance_id = instance.get("InstanceId")
            current_type = instance.get("InstanceType")
            state = (instance.get("State") or {}).get("Name", "unknown")
            tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}

            if instance_id in seen_instance_ids:
                print(
                    f"Skipping duplicate entry for {instance_id}; already queued in this run."
                )
                continue
            seen_instance_ids.add(instance_id)

            if not instance_matches_schedule(tags, schedule_name):
                skipped_due_to_schedule += 1
                print(
                    f"Skipping {instance_id} because schedule tag did not match '{schedule_name}'."
                )
                continue

            instances_to_process.append(
                {
                    "instance": instance,
                    "instance_id": instance_id,
                    "current_type": current_type,
                    "state": state,
                    "tags": tags,
                }
            )

    if not instances_to_process:
        print(
            "No EC2 instances matched the requested schedule. Skipping workflow."
        )
        return {
            "processed_instances": 0,
            "action": action,
            "schedule": schedule_name,
            "skipped_instances": skipped_due_to_schedule,
        }

    batch_size = BATCH_SIZE if BATCH_SIZE > 0 else 1
    concurrency = get_concurrency_limit()
    print(
        f"Processing {len(instances_to_process)} instances with "
        f"concurrency={concurrency} batch_size={batch_size} for schedule '{schedule_name}'."
    )

    downsize_type = get_downsize_type() if action == "scaledown" else None
    savings_records = []
    actual_savings_records = []
    processed_count = 0

    for batch in chunks(instances_to_process, batch_size):
        print(f"Processing batch of {len(batch)} instance(s)...")
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_to_instance = {
                executor.submit(
                    process_instance,
                    instance_data,
                    action,
                    run_start_timestamp,
                    downsize_type,
                ): instance_data["instance_id"]
                for instance_data in batch
            }

            for future in as_completed(future_to_instance):
                instance_id = future_to_instance[future]
                try:
                    processed, savings_record, actual_record = future.result()
                except Exception as exc:
                    print(f"Error processing {instance_id}: {exc}")
                    if FAIL_FAST:
                        raise
                    continue

                if processed:
                    processed_count += 1
                if savings_record:
                    savings_records.append(savings_record)
                if actual_record:
                    actual_savings_records.append(actual_record)

    print("Lambda execution completed.")

    if action == "scaledown":
        record_savings(savings_records)
    else:
        record_actual_savings(actual_savings_records)

    return {
        "processed_instances": processed_count,
        "action": action,
        "schedule": schedule_name,
        "skipped_instances": skipped_due_to_schedule,
    }

