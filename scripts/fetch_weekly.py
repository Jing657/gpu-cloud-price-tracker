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
    rows = []
    for inst,(gpu,n) in AZURE_TYPES.items():
        filt = "serviceName eq 'Virtual Machines' and armRegionName eq 'eastus' and armSkuName eq '%s' and priceType eq 'Consumption'" % inst
        url = AZURE_BASE+'?'+urllib.parse.urlencode({'$filter':filt,'currencyCode':'USD'})
        nxt,seen = url,set()
        while nxt:
            if nxt in seen:
                raise ValueError('Azure pagination loop')
            seen.add(nxt)
            payload = get_json(nxt)
            for item in payload.get('Items',[]):
                if item.get('armSkuName') != inst or item.get('armRegionName') != 'eastus':
                    continue
                if item.get('type') != 'Consumption' or item.get('currencyCode') != 'USD' or item.get('unitOfMeasure') != '1 Hour':
                    continue
                if item.get('isPrimaryMeterRegion') is False:
                    continue
                desc = ' '.join(str(item.get(k,'')) for k in ('productName','skuName','meterName')).lower()
                if any(t in desc for t in ('spot','low priority','windows','promo')):
                    continue
                try:
                    p = float(item['retailPrice'])
                except (KeyError,ValueError,TypeError):
                    continue
                if p > 0:
                    rows.append(quote('azure',gpu,'eastus',inst,n,p,url,item.get('meterId',''),item.get('effectiveStartDate','')))
            nxt = payload.get('NextPageLink')
    return dedup(rows,'Azure')


def gcp_quotes():
    """Official Cloud Billing Catalog; requires GOOGLE_CLOUD_BILLING_API_KEY secret.
    Only standalone GPU hourly SKUs, excluding commitments/preemptible/spot.
    """
    key = os.environ.get('GOOGLE_CLOUD_BILLING_API_KEY','').strip()
    if not key:
        raise RuntimeError('GOOGLE_CLOUD_BILLING_API_KEY not configured; GCP official SKU prices require a Google Cloud API key')
    url = GCP_BASE+'?'+urllib.parse.urlencode({'key':key,'currencyCode':'USD','pageSize':5000})
    rows,token = [],''
    while True:
        next_url = url + ('&pageToken='+urllib.parse.quote(token) if token else '')
        payload = get_json(next_url)
        for sku in payload.get('skus',[]):
            cat = sku.get('category',{})
            if cat.get('resourceFamily') != 'Compute' or cat.get('usageType') != 'OnDemand':
                continue
            desc = sku.get('description','')
            low = desc.lower()
            if not ('gpu' in low or 'nvidia' in low):
                continue
            if any(x in low for x in ('spot','preemptible','commitment','reservation','ram','cpu','interconnect')):
                continue
            gpu = next((g for g in ('H100','H200','B200','B300') if re.search(r'(?<![A-Z0-9])'+g+r'(?![A-Z0-9])',desc,re.I)),None)
            if not gpu:
                continue
            regions = sku.get('serviceRegions',[])
            if 'us-central1' not in regions:
                continue
            pi = sku.get('pricingInfo',[])
            if not pi:
                continue
            expr = pi[-1].get('pricingExpression',{})
            if expr.get('usageUnit') not in ('h','hour'):
                continue
            tiers = expr.get('tieredRates',[])
            if len(tiers) != 1 or float(tiers[0].get('startUsageAmount',-1)) != 0:
                continue
            money = tiers[0].get('unitPrice',{})
            if money.get('currencyCode') != 'USD':
                continue
            p = float(money.get('units',0))+int(money.get('nanos',0))/1e9
            if p > 0:
                rows.append(quote('google_cloud',gpu,'us-central1',desc,1,p,
                    'https://cloud.google.com/compute/gpus-pricing',sku.get('skuId',''),pi[-1].get('effectiveTime',''),
                    pricing_type='on_demand_gpu_only'))
        token = payload.get('nextPageToken','')
        if not token:
            break
    return dedup(rows,'Google Cloud')


def oracle_quotes():
    """No price inferred from localized currency or blank cells.
    Official Oracle USD page currently exposes shapes but may omit numeric rates.
    """
    html = request_bytes(ORACLE_URL).decode('utf-8','replace')
    text = unescape(re.sub(r'<[^>]+>',' ',html))
    if 'BM.GPU.H100.8' not in text:
        raise RuntimeError('Oracle official pricing page structure changed')
    # Deliberately do not parse unstructured numbers: Oracle's regional pages
    # can show non-USD amounts and its US page may omit numeric rates.
    return []


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
