"""Weekly US AWS/Azure on-demand GPU price snapshots.

Preserves existing monthly CSV and 14-column weekly CSV contract.
Uses public AWS offer JSON and Azure Retail Prices API; no credentials required.
"""
import csv
import datetime as dt
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
OUT = DATA / 'weekly_prices.csv'
STATUS = DATA / 'weekly_update_status.csv'
FIELDS = ['observed_at_utc','week_start','provider','gpu','region','instance_type','gpu_count','vm_usd_per_hour','usd_per_gpu_hour','pricing_type','source_url','source_sku','source_effective_date','status']
AWS_TYPES = {'p5.48xlarge': ('H100', 8), 'p5e.48xlarge': ('H200', 8)}
AZURE_TYPES = {'Standard_ND96isr_H100_v5': ('H100', 8), 'Standard_ND96isr_H200_v5': ('H200', 8)}
AWS_URL = 'https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.json'
AZURE_BASE = 'https://prices.azure.com/api/retail/prices'


def get_json(url, timeout=240):
    request = urllib.request.Request(url, headers={'User-Agent':'GPU-Price-Research/1.1', 'Accept':'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def aws_quotes():
    """Read public AWS regional offer, matching products to OnDemand terms by SKU."""
    data = get_json(AWS_URL, timeout=300)
    products = data.get('products', {})
    terms = data.get('terms', {}).get('OnDemand', {})
    candidates = {}
    for sku, product in products.items():
        attrs = product.get('attributes', {})
        instance = attrs.get('instanceType', '')
        if instance not in AWS_TYPES:
            continue
        if attrs.get('operatingSystem') != 'Linux' or attrs.get('tenancy') != 'Shared':
            continue
        if attrs.get('preInstalledSw', 'NA') not in ('NA', ''):
            continue
        if attrs.get('capacitystatus', 'Used') not in ('Used', ''):
            continue
        if product.get('productFamily') != 'Compute Instance':
            continue
        usage = attrs.get('usagetype', attrs.get('usageType', ''))
        if 'BoxUsage' not in usage:
            continue
        candidates[sku] = instance
    print(f'AWS matched product SKUs: {len(candidates)}; instance types: {sorted(set(candidates.values()))}', flush=True)
    quotes = []
    for sku, instance in candidates.items():
        for offer in terms.get(sku, {}).values():
            effective = offer.get('effectiveDate', '')
            for dimension in offer.get('priceDimensions', {}).values():
                if dimension.get('unit') not in ('Hrs', 'Hours'):
                    continue
                try:
                    price = float(dimension.get('pricePerUnit', {}).get('USD', ''))
                except (ValueError, TypeError):
                    continue
                if price <= 0:
                    continue
                gpu, count = AWS_TYPES[instance]
                quotes.append(dict(provider='aws', gpu=gpu, region='us-east-1', instance_type=instance,
                    gpu_count=count, vm_usd_per_hour=price, usd_per_gpu_hour=price/count,
                    pricing_type='on_demand', source_url=AWS_URL, source_sku=sku,
                    source_effective_date=effective, status='ok'))
    return deduplicate(quotes, 'AWS')


def azure_quotes():
    quotes = []
    for sku, (gpu, count) in AZURE_TYPES.items():
        query = "serviceName eq 'Virtual Machines' and armRegionName eq 'eastus' and armSkuName eq '%s' and priceType eq 'Consumption'" % sku
        url = AZURE_BASE + '?' + urllib.parse.urlencode({'$filter': query, 'currencyCode': 'USD'})
        next_url = url
        seen = set()
        while next_url:
            if next_url in seen:
                raise RuntimeError('Azure pagination loop')
            seen.add(next_url)
            payload = get_json(next_url)
            for item in payload.get('Items', []):
                if item.get('armSkuName') != sku or item.get('armRegionName') != 'eastus':
                    continue
                if item.get('type') != 'Consumption' or item.get('currencyCode') != 'USD':
                    continue
                if item.get('unitOfMeasure') != '1 Hour':
                    continue
                if item.get('isPrimaryMeterRegion') is False:
                    continue
                description = ' '.join(str(item.get(k, '')) for k in ('productName', 'skuName', 'meterName')).lower()
                if any(term in description for term in ('spot', 'low priority', 'windows', 'promo')):
                    continue
                try:
                    price = float(item['retailPrice'])
                except (KeyError, ValueError, TypeError):
                    continue
                if price <= 0:
                    continue
                quotes.append(dict(provider='azure', gpu=gpu, region='eastus', instance_type=sku,
                    gpu_count=count, vm_usd_per_hour=price, usd_per_gpu_hour=price/count,
                    pricing_type='on_demand', source_url=url, source_sku=item.get('meterId', ''),
                    source_effective_date=item.get('effectiveStartDate', ''), status='ok'))
            next_url = payload.get('NextPageLink')
    return deduplicate(quotes, 'Azure')


def deduplicate(rows, name):
    unique = {}
    for row in rows:
        key = (row['provider'], row['region'], row['instance_type'])
        if key in unique and abs(float(unique[key]['vm_usd_per_hour']) - float(row['vm_usd_per_hour'])) > 0.000001:
            raise RuntimeError(f'{name} conflicting rates for {key}; inspect source meters')
        unique[key] = row
    print(f'{name} verified quotes: {len(unique)}', flush=True)
    return list(unique.values())


def main():
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.isoformat(timespec='seconds')
    week = (now.date() - dt.timedelta(days=now.weekday())).isoformat()
    prior = []
    if OUT.exists():
        with OUT.open(newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != FIELDS:
                raise ValueError('Existing weekly CSV schema mismatch; refusing overwrite')
            prior = list(reader)
    rows, statuses = [], []
    for name, function in [('aws', aws_quotes), ('azure', azure_quotes)]:
        try:
            observed = function()
            statuses.append((name, 'ok' if observed else 'no_matching_sku', len(observed), '', stamp))
            rows.extend(observed)
        except Exception as exc:
            statuses.append((name, 'error', 0, str(exc)[:300], stamp))
    for row in rows:
        row.update(observed_at_utc=stamp, week_start=week)
    keys = {(r['week_start'], r['provider'], r['region'], r['instance_type']) for r in rows}
    prior = [r for r in prior if (r['week_start'], r['provider'], r['region'], r['instance_type']) not in keys]
    merged = sorted(prior + rows, key=lambda r:(r['week_start'], r['provider'], r['gpu'], r['region'], r['instance_type']))
    tmp = OUT.with_suffix('.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(merged)
    os.replace(tmp, OUT)
    with STATUS.open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['provider', 'status', 'quote_count', 'error', 'checked_at_utc'])
        writer.writerows(statuses)
    for item in statuses:
        print(*item, sep=' | ', flush=True)
    if any(s[1] == 'error' for s in statuses):
        raise SystemExit('One or more API sources failed; check status CSV')
    if not rows:
        raise SystemExit('No verified prices found; review source SKU mappings')


if __name__ == '__main__':
    main()
