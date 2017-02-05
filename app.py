
import bottle
import json
import requests
from fuzzywuzzy import fuzz
from labelstore import LabelStore
from typematcher import TypeMatcher

from bottle import route, run, request, default_app, template, HTTPError
from docopt import docopt
import redis

### CONFIG ###
max_results = 20
service_name = 'Wikidata Reconciliation for OpenRefine'

wd_api_search_results = 10 # max 50

redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
### END CONFIG ###


headers = {
    'User-Agent':service_name,
}

label_store = LabelStore(redis_client)
type_matcher = TypeMatcher(redis_client)

def search_wikidata(query, default_language='en'):
    print(query)

    search_string = query['query']
    properties = query.get('properties', [])
    target_types = query.get('type', [])
    if type(target_types) != list:
        target_types = [target_types]

    # search using the target label as search string
    r = requests.get(
        'https://www.wikidata.org/w/api.php',
        {'action':'query',
         'format':'json',
         'list':'search',
         'namespace':0,
         'srlimit':wd_api_search_results,
         'srsearch':search_string},
        headers=headers)
    print(r.url)
    resp = r.json()
    ids = [item['title'] for item in resp.get('query', {}).get('search')]

    # retrieve corresponding items
    r = requests.get(
        'https://www.wikidata.org/w/api.php',
        {'action':'wbgetentities',
         'format':'json',
         'ids':'|'.join(ids)})
    resp = r.json()
    items = resp.get('entities', {})

    # Add the label as "yet another property"
    properties_with_label = [{'pid':'label','v':query['query']}]+properties

    scored_items = []
    types_to_prefetch = set()
    for qid, item in items.items():
        simplified = {'qid':qid}

        # Add labels
        labels = set()
        for lang, lang_label in item.get('labels', {}).items():
            labels.add(lang_label['value'])

        # Add aliases
        for lang, lang_aliases in item.get('aliases', {}).items():
            for lang_alias in lang_aliases:
                labels.add(lang_alias['value'])
        simplified['label'] = list(labels)

        # Add other properties
        prop_ids = ['P31'] # instance of
        for prop in properties:
            if 'pid' not in prop:
                raise ValueError("Property id ('pid') not provided")
            prop_ids.append(prop['pid'])

        for prop_id in prop_ids:
            claims = item.get('claims', {}).get(prop_id, [])
            values = set()
            for claim in claims:
                val = claim.get('mainsnak', {}).get('datavalue', {}).get('value')
                if type(val) == dict:
                    if val.get('entity-type') == 'item':
                        values.add(val.get('id'))
                else:
                    values.add(str(val))
            simplified[prop_id] = list(values)

        # Check the type if we have a type constraint
        if target_types:
            current_types = simplified['P31']
            found = any([
                any([
                    type_matcher.is_subclass(typ, target_type)
                    for typ in current_types
                ])
                for target_type in target_types])

            if not found:
                print("skipping item")
                print(current_types)
                print(simplified['label'])
                continue

        # Compute per-property score
        scored = {}
        matching_fun = fuzz.ratio
        for prop in properties_with_label:
            prop_id = prop['pid']
            ref_val = prop['v']

            maxscore = 0
            bestval = None
            values = simplified.get(prop_id, [])
            for val in values:
                curscore = matching_fun(val, ref_val)
                if curscore > maxscore or bestval is None:
                    bestval = val
                    maxscore = curscore

            scored[prop_id] = {
                'values': values,
                'best_value': bestval,
                'score': maxscore,
            }

        # Compute overall score
        nonzero_scores = [
            prop['score'] for pid, prop in scored.items()
            if prop['score'] > 0 ]
        if nonzero_scores:
            avg = sum(nonzero_scores) / float(len(nonzero_scores))
        else:
            avg = 0
        scored['score'] = avg

        scored['id'] = qid
        scored['name'] = scored['label'].get('best_value', '')
        scored['type'] = simplified['P31']
        types_to_prefetch |= set(simplified['P31'])
        scored['match'] = False

        scored_items.append(scored)

    # Prefetch the labels for the types
    label_store.prefetch_labels(list(types_to_prefetch), default_language)

    # Add the labels to the response
    for i in range(len(scored_items)):
        scored_items[i]['type'] = [
            {'id':id, 'name':label_store.get_label(id, lang)}
                for id in scored_items[i]['type']]

    return sorted(scored_items, key=lambda i: -i.get('score', 0))

def perform_query(q):
    type_strict = q.get('type_strict', 'any')
    if type_strict not in ['any','all','should']:
        raise ValueError('Invalid type_strict')
    return search_wikidata(q)

@route('/api', method=['GET','POST'])
def api():
    callback = request.query.get('callback') or request.forms.get('callback')
    query = request.query.get('query') or request.forms.get('query')
    queries = request.query.get('queries') or request.forms.queries
    print(queries)
    if query:
        try:
            query = json.loads(query)
            result = [perform_query(query)]
            return {'result':result}
        except ValueError as e:
            return {'status':'error',
                    'message':'invalid query',
                    'details': str(e)}
    elif queries:
        try:
            queries = json.loads(queries)
            result = { k:{'result':perform_query(q)} for k, q in queries.items() }
            return result
        except (ValueError, AttributeError, KeyError) as e:
            print(e)
            return {'status':'error',
                    'message':'invalid query',
                    'details': str(e)}

    else:
        identify = {
            'name':service_name,
            'view':{'url':'https://www.wikidata.org/wiki/{{id}}'},
            }
        if callback:
            return '%s(%s);' % (callback, json.dumps(identify))
        return identify

@route('/')
def home():
    with open('templates/index.html', 'r') as f:
        return template(f.read())

if __name__ == '__main__':
    run(host='localhost', port=8000, debug=True)

app = application = default_app()
