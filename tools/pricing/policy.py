"""Bounds and read-only allowlist shared by the MCP Pricing backend."""
READ_TOOLS = frozenset({'get_pricing', 'get_pricing_service_codes', 'get_pricing_service_attributes',
                       'get_pricing_attribute_values', 'get_price_list_urls'})


def pricing_arguments(name, arguments):
    if name not in READ_TOOLS:
        raise ValueError('Unsupported read-only Pricing tool')
    args = dict(arguments)
    if 'regions' in args:
        regions = args.pop('regions')
        if 'region' in args or not isinstance(regions, list) or not 1 <= len(regions) <= 3:
            raise ValueError('Specify region or at most three regions')
        args['region'] = regions
    if name == 'get_pricing':
        args.setdefault('region', 'cn-northwest-1')
        args['max_results'] = min(10, max(1, int(args.get('max_results', 10))))
        args['max_allowed_characters'] = min(16000, max(1000, int(args.get('max_allowed_characters', 12000))))
        args.setdefault('output_options', {'pricing_terms': ['OnDemand']})
    return args
