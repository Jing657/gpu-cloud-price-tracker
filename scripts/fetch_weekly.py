                continue
            if price <= 0:
                continue
            counts['positive_hourly'] += 1
            gpu, gpu_count = AWS_TYPES[typ]
            quotes.append(dict(provider='aws', gpu=gpu, region='us-east-1',
                               instance_type=typ, gpu_count=gpu_count,
                               vm_usd_per_hour=price,
                               usd_per_gpu_hour=price / gpu_count,
                               pricing_type='on_demand', source_url=AWS_URL,
                               source_sku=field(r, 'SKU'),
                               source_effective_date=field(r, 'EffectiveDate', 'Effective Date'),
                               status='ok'))
    unique = {}
    for q in quotes:
        key = (q['region'], q['instance_type'])
        if key in unique and abs(unique[key]['vm_usd_per_hour'] - q['vm_usd_per_hour']) > 1e-8:
            raise RuntimeError(f'AWS conflicting prices for {key}')
        unique[key] = q
    print('AWS candidate instance types:', sorted(candidates), flush=True)
    print('AWS filter counts:', counts, 'valid quotes:', len(unique), flush=True)
    return list(unique.values())


def azure_quotes():
    quotes = []
    for sku, (gpu, count) in AZURE_TYPES.items():
        filter_expr = ("serviceName eq 'Virtual Machines' and armRegionName eq 'eastus' "
                       "and armSkuName eq '%s' and priceType eq 'Consumption'" % sku)
        url = AZURE_BASE + '?' + urllib.parse.urlencode({'$filter': filter_expr, 'currencyCode': 'USD'})
        next_url, seen = url, set()
        while next_url:
            if next_url in seen:
                raise RuntimeError('Azure pagination loop')
            seen.add(next_url)
            with fetch(next_url) as response:
                payload = json.load(response)
            for item in payload.get('Items', []):
                if item.get('armSkuName') != sku or item.get('armRegionName') != 'eastus':
                    continue
                if item.get('type') != 'Consumption' or item.get('currencyCode') != 'USD':
                    continue
                if item.get('unitOfMeasure') != '1 Hour':
                    continue
                if item.get('isPrimaryMeterRegion') is False:
                    continue
                desc = ' '.join(str(item.get(k, '')) for k in ('productName', 'skuName', 'meterName'))
                if any(x in desc.lower() for x in ('spot', 'low priority', 'windows', 'promo')):
                    continue
                try:
                    price = float(item['retailPrice'])
                except (KeyError, TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                quotes.append(dict(provider='azure', gpu=gpu, region='eastus',
                                   instance_type=sku, gpu_count=count,
                                   vm_usd_per_hour=price, usd_per_gpu_hour=price / count,
                                   pricing_type='on_demand', source_url=url,
                                   source_sku=item.get('meterId', ''),
                                   source_effective_date=item.get('effectiveStartDate', ''),
                                   status='ok'))
            next_url = payload.get('NextPageLink')
    unique = {}
    for q in quotes:
        key = (q['region'], q['instance_type'])
        if key in unique and abs(unique[key]['vm_usd_per_hour'] - q['vm_usd_per_hour']) > 1e-8:
            raise RuntimeError(f'Azure conflicting prices for {key}')
        unique[key] = q
    print('Azure valid quotes:', len(unique), flush=True)
    return list(unique.values())


def main():
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.isoformat(timespec='seconds')
    week = (now.date() - dt.timedelta(days=now.weekday())).isoformat()
    previous = []
    if OUT.exists():
        with OUT.open(newline='', encoding='utf-8-sig') as f:
            previous = list(csv.DictReader(f))
    observations, statuses = [], []
    for name, collector in [('aws', aws_quotes), ('azure', azure_quotes)]:
        try:
            found = collector()
            statuses.append((name, 'ok' if found else 'no_matching_sku', len(found), '', stamp))
            for q in found:
                q.update(observed_at_utc=stamp, week_start=week)
            observations.extend(found)
        except Exception as exc:
            statuses.append((name, 'error', 0, str(exc)[:300], stamp))
    new_keys = {(r['week_start'], r['provider'], r['region'], r['instance_type'])
                for r in observations}
    previous = [r for r in previous if
                (r['week_start'], r['provider'], r['region'], r['instance_type']) not in new_keys]
    combined = previous + observations
    combined.sort(key=lambda r: (r['week_start'], r['provider'], r['gpu'],
                                 r['region'], r['instance_type']))
    temp = OUT.with_suffix('.tmp')
    with temp.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(combined)
    os.replace(temp, OUT)
    with STATUS.open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['provider', 'status', 'quote_count', 'error', 'checked_at_utc'])
        writer.writerows(statuses)
    for row in statuses:
        print(*row, sep=' | ', flush=True)
    if any(s[1] == 'error' for s in statuses):
        raise SystemExit('One or more API sources failed; check status CSV')
    if not observations:
        raise SystemExit('No verified prices found; review SKU mappings and API response')


if __name__ == '__main__':
    main()
