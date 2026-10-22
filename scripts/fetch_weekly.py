"""Weekly GPU prices: AWS/Azure official public APIs; GCP billing API (key required);
Oracle official price-list extraction when explicit USD pricing is exposed.
Preserves existing 14-column weekly_prices.csv and monthly history unchanged.
"""
import csv
import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
DATA.mkdir(parents=True, exist_ok=True)
OUT = DATA / 'weekly_prices.csv'
STATUS = DATA / 'weekly_update_status.csv'
FIELDS = ['observed_at_utc','week_start','provider','gpu','region','instance_type','gpu_count','vm_usd_per_hour','usd_per_gpu_hour','pricing_type','source_url','source_sku','source_effective_date','status']
AWS_TYPES = {'p5.48xlarge': ('H100',8), 'p5e.48xlarge': ('H200',8), 'p5en.48xlarge': ('H200',8), 'p6-b200.48xlarge': ('B200',8), 'p6-b300.48xlarge': ('B300',8)}
AZURE_TYPES = {'Standard_ND96isr_H100_v5':('H100',8), 'Standard_ND96isr_H200_v5':('H200',8), 'Standard_ND96isr_B200_v6':('B200',8)}
AWS_URL = 'https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.json'
AZURE_BASE = 'https://prices.azure.com/api/retail/prices'
GCP_BASE = 'https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus'
ORACLE_URL = 'https://www.oracle.com/cloud/price-list/'


def request_bytes(url, timeout=300):
    req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 GPU-price-research/2.0','Accept':'application/json,text/html'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_json(url):
    return json.loads(request_bytes(url))


def dedup(rows, provider):
    unique = {}
    for row in rows:
        key = (row['provider'],row['region'],row['instance_type'],row['pricing_type'])
        if key in unique and abs(float(unique[key]['usd_per_gpu_hour'])-float(row['usd_per_gpu_hour'])) > 1e-6:
            raise ValueError(f'{provider}: conflicting prices for {key}')
        unique[key] = row
    return list(unique.values())


def quote(provider, gpu, region, instance, count, price, url, sku='', effective='', pricing_type='on_demand'):
    return dict(provider=provider,gpu=gpu,region=region,instance_type=instance,gpu_count=count,
        vm_usd_per_hour=round(price,8),usd_per_gpu_hour=round(price/count,8),pricing_type=pricing_type,
        source_url=url,source_sku=sku,source_effective_date=effective,status='ok')


def aws_quotes():
    data = get_json(AWS_URL)
    candidates = {}
    for sku, item in data.get('products',{}).items():
        a = item.get('attributes',{})
        inst = a.get('instanceType','')
        if inst not in AWS_TYPES or a.get('operatingSystem') != 'Linux' or a.get('tenancy') != 'Shared':
            continue
        if a.get('preInstalledSw','NA') not in ('NA','') or a.get('capacitystatus','Used') not in ('Used',''):
            continue
        if item.get('productFamily') != 'Compute Instance' or 'BoxUsage' not in a.get('usagetype',a.get('usageType','')):
            continue
        candidates[sku] = inst
    rows = []
    for sku, inst in candidates.items():
        for term in data.get('terms',{}).get('OnDemand',{}).get(sku,{}).values():
            for dim in term.get('priceDimensions',{}).values():
                if dim.get('unit') not in ('Hrs','Hours'):
                    continue
                try:
                    p = float(dim.get('pricePerUnit',{}).get('USD',''))
                except (ValueError,TypeError):
                    continue
                if p > 0:
                    gpu,n = AWS_TYPES[inst]
                    rows.append(quote('aws',gpu,'us-east-1',inst,n,p,AWS_URL,sku,term.get('effectiveDate','')))
    return dedup(rows,'AWS')


def azure_quotes():
    """Official Azure Retail API; search multiple regions for documented GPU shapes."""
    rows = []
    # GPU counts must be verified against Microsoft VM-size documentation.
    sizes = {
        'Standard_ND96isr_H100_v5': ('H100', 8),
        'Standard_ND96isr_H200_v5': ('H200', 8),
    }
    regions = ('eastus', 'eastus2', 'southcentralus', 'westus2', 'westus3',
               'centralus', 'northcentralus', 'swedencentral', 'westeurope')
    for inst, (gpu, n) in sizes.items():
        for region in regions:
            filt = ("serviceName eq 'Virtual Machines' and armRegionName eq '%s' "
                    "and armSkuName eq '%s' and priceType eq 'Consumption'") % (region, inst)
            url = AZURE_BASE + '?' + urllib.parse.urlencode({'$filter': filt, 'currencyCode': 'USD'})
            nxt, seen = url, set()
            while nxt:
                if nxt in seen:
                    raise ValueError('Azure pagination loop')
                seen.add(nxt)
                payload = get_json(nxt)
                for item in payload.get('Items', []):
                    if item.get('armSkuName') != inst or item.get('armRegionName') != region:
                        continue
                    if item.get('type') != 'Consumption' or item.get('currencyCode') != 'USD':
                        continue
                    if item.get('unitOfMeasure') != '1 Hour' or item.get('isPrimaryMeterRegion') is False:
                        continue
                    desc = ' '.join(str(item.get(k,'')) for k in ('productName','skuName','meterName')).lower()
                    if any(x in desc for x in ('spot','low priority','windows','promo')):
                        continue
                    try:
                        price = float(item['retailPrice'])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if price > 0:
                        rows.append(quote('azure', gpu, region, inst, n, price,
                                          url, item.get('meterId',''), item.get('effectiveStartDate','')))
                nxt = payload.get('NextPageLink')
    return dedup(rows, 'Azure')


def classify_gcp_sku(description):
    """Return comparable GPU-only on-demand vs special purchasing programs."""
    low = description.lower()
    if any(x in low for x in ('spot', 'preemptible', 'commitment', 'committed use')):
        return None
    if 'dws' in low or 'defined duration' in low:
        return 'dws_gpu_only'
    if 'reserved' in low or 'calendar mode' in low or 'reservation' in low:
        return 'reserved_gpu_only'
    return 'on_demand_gpu_only'


def gcp_quotes():
    """Collect GCP GPU-only SKU prices, preserving the purchasing program.

    Catalog category usageType alone is insufficient: DWS/Reserved GPU SKUs
    can also report OnDemand. Their descriptions must be classified separately.
    """
    key = os.environ.get('GOOGLE_CLOUD_BILLING_API_KEY', '').strip()
    if not key:
        raise RuntimeError('GOOGLE_CLOUD_BILLING_API_KEY not configured; GCP official SKU prices require a Google Cloud API key')
    base = GCP_BASE + '?' + urllib.parse.urlencode({'key': key, 'currencyCode': 'USD', 'pageSize': 5000})
    rows, token = [], ''
    while True:
        next_url = base + ('&pageToken=' + urllib.parse.quote(token) if token else '')
        payload = get_json(next_url)
        for sku in payload.get('skus', []):
            cat = sku.get('category', {})
            if cat.get('resourceFamily') != 'Compute' or cat.get('usageType') != 'OnDemand':
                continue
            desc = sku.get('description', '')
            low = desc.lower()
            if not ('gpu' in low or 'nvidia' in low):
                continue
            if any(x in low for x in ('ram', 'cpu', 'interconnect')):
                continue
            pricing_type = classify_gcp_sku(desc)
            if pricing_type is None:
                continue
            gpu = next((g for g in ('H100', 'H200', 'B200', 'B300')
                        if re.search(r'(?<![A-Z0-9])' + g + r'(?![A-Z0-9])', desc, re.I)), None)
            if not gpu or 'us-central1' not in sku.get('serviceRegions', []):
                continue
            infos = sku.get('pricingInfo', [])
            if not infos:
                continue
            info = infos[-1]
            expr = info.get('pricingExpression', {})
            if expr.get('usageUnit') not in ('h', 'hour'):
                continue
            tiers = expr.get('tieredRates', [])
            if len(tiers) != 1 or float(tiers[0].get('startUsageAmount', -1)) != 0:
                continue
            money = tiers[0].get('unitPrice', {})
            if money.get('currencyCode') != 'USD':
                continue
            price = float(money.get('units', 0)) + int(money.get('nanos', 0)) / 1e9
            if price > 0:
                rows.append(quote('google_cloud', gpu, 'us-central1', desc, 1, price,
                    'https://cloud.google.com/compute/gpus-pricing', sku.get('skuId', ''),
                    info.get('effectiveTime', ''), pricing_type=pricing_type))
        token = payload.get('nextPageToken', '')
        if not token:
            break
    return dedup(rows, 'Google Cloud')


def oracle_quotes():
    """Parse Oracle's official USD global price-list PDF, matching exact part numbers.

    This is NOT a hardcoded price feed: prices are read afresh from the PDF.
    Unknown or changed PDF layouts fail closed instead of recording wrong rates.
    """
    import io
    import pdfplumber

    url = ('https://www.oracle.com/tw/a/ocom/docs/corporate/pricing/'
           'oracle-paas-and-iaas-global-price-list.pdf')
    blob = request_bytes(url, timeout=90)
    if not blob.startswith(b'%PDF'):
        raise ValueError('Oracle source is not a PDF')
    # These IDs are official compute GPU part numbers (NOT AI Enterprise licences).
    products = {
        'H100': ('B98415', 'BM.GPU.H100.8', 8),
        'H200': ('B110519', 'BM.GPU.H200.8', 8),
        'B200': ('B110978', 'BM.GPU.B200.8', 8),
        'B300': ('B112237', 'BM.GPU.B300.8', 8),
    }
    rows = []
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        intro = (pdf.pages[0].extract_text() or '')
        if 'Global Price List' not in intro:
            raise ValueError('Unexpected Oracle PDF title')
        effective = ''
        match_date = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+20\d{2}', intro)
        if match_date:
            effective = match_date.group(0)
        if not any('Prices in US Dollar' in (page.extract_text() or '') for page in pdf.pages[:3]):
            raise ValueError('Oracle PDF does not explicitly state USD')
        for gpu, (part, shape, count) in products.items():
            candidates = []
            for page in pdf.pages:
                txt = page.extract_text(layout=False) or ''
                if part not in txt:
                    continue
                for line in txt.splitlines():
                    if not re.search(r'\b' + re.escape(part) + r'\b', line):
                        continue
                    # PDF columns may reorder, but a matching row must have
                    # two identical Pay-as-you-go and commitment dollar rates.
                    numbers = re.findall(r'(?<![\w.])\d+(?:\.\d+)?(?![\w.])', line.replace(part, ''))
                    if len(numbers) >= 2 and numbers[0] == numbers[1]:
                        val = float(numbers[0])
                        if 0 < val < 200:
                            candidates.append(val)
            if not candidates or len({round(x, 8) for x in candidates}) != 1:
                raise ValueError('Cannot uniquely verify Oracle USD rate for %s (%s): %r' % (gpu, part, candidates))
            price = candidates[0]
            rows.append(quote('oracle', gpu, 'global_list_price', shape, count,
                              price * count, url, part, effective,
                              pricing_type='on_demand'))
    return rows


def main():
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.isoformat(timespec='seconds')
    week = (now.date()-dt.timedelta(days=now.weekday())).isoformat()
    prior = []
    if OUT.exists():
        with OUT.open(newline='',encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != FIELDS:
                raise ValueError('Existing weekly CSV schema mismatch; refusing overwrite')
            prior = list(reader)
    # Correct previously mislabelled GCP observations, including DWS and
    # Reserved rows from earlier runs. Do not change historical prices/dates.
    for old in prior:
        if old.get('provider') == 'google_cloud' and old.get('pricing_type') == 'on_demand_gpu_only':
            classified = classify_gcp_sku(old.get('instance_type', ''))
            if classified:
                old['pricing_type'] = classified
    rows,statuses = [],[]
    for name,func in [('aws',aws_quotes),('azure',azure_quotes),('google_cloud',gcp_quotes),('oracle',oracle_quotes)]:
        try:
            observed = func()
            status = 'ok' if observed else 'no_verified_usd_quote'
            statuses.append((name,status,len(observed),'',stamp))
            rows.extend(observed)
        except Exception as exc:
            statuses.append((name,'error',0,str(exc)[:300],stamp))
    for row in rows:
        row.update(observed_at_utc=stamp,week_start=week)
    keys = {(r['week_start'],r['provider'],r['region'],r['instance_type'],r['pricing_type']) for r in rows}
    # A successful provider refresh replaces that provider's current-week
    # observations; on error, keep the last good snapshot instead.
    refreshed = {name for name, status, count, err, checked in statuses if status == 'ok'}
    prior = [r for r in prior if not (r['week_start'] == week and r['provider'] in refreshed)]
    prior = [r for r in prior if (r['week_start'],r['provider'],r['region'],r['instance_type'],r['pricing_type']) not in keys]
    merged = sorted(prior+rows,key=lambda r:(r['week_start'],r['provider'],r['gpu'],r['region'],r['instance_type']))
    tmp = OUT.with_suffix('.tmp')
    with tmp.open('w',newline='',encoding='utf-8') as f:
        writer = csv.DictWriter(f,fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(merged)
    os.replace(tmp,OUT)
    with STATUS.open('w',newline='',encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['provider','status','quote_count','error','checked_at_utc'])
        writer.writerows(statuses)
    for s in statuses:
        print(*s,sep=' | ',flush=True)
    if not rows:
        raise SystemExit('No verified prices; inspect weekly_update_status.csv')
    # Optional providers do not block successfully collected prices.
    if any(s[0] in ('aws','azure') and s[1] == 'error' for s in statuses):
        raise SystemExit('AWS or Azure failed; saved other quotes and status')


if __name__ == '__main__':
    main()
