"""Fast local Upbeat treatment planning. No provider calls or audio-quality claims."""
from __future__ import annotations
import hashlib, json, random, secrets
from pathlib import Path
from core import atomic, canonical, digest, lock, private, read, require, utc

VERSION='upbeat-director-v1'
ELIGIBLE=('guitar_solo','piano_solo','alternating_duet','suno_freedom')
RULES='Sing every supplied Scripture word once in order. No added words, echoes, paraphrases, repeats or spoken narration. Preserve the exact translation and divine names. Full chapter; do not truncate.'


def input_identity(request,recipe,lyrics):
    fields=('book','chapter','genre','edition','target','sourceSha256')
    return digest(canonical({'chapter':{k:request.get(k) for k in fields},
                             'recipe':recipe,'lyricsSha256':digest(lyrics.encode())}))


def chapter_features(job,lyrics,request):
    source=job/'source-verses.json'
    verses=read(source).get('verses',[]) if source.exists() else []
    words=len(lyrics.split())
    supplied=request.get('chapter_context')
    context=supplied if supplied in ('praise','narrative','lament','wisdom','dense') else 'unknown'
    dense=words>=650 or (bool(verses) and words/len(verses)>=35)
    return {'wordCount':words,'verseCount':len(verses),'dense':dense,'context':context,
            'contextBasis':'explicit_request_metadata' if context!='unknown' else 'semantic_context_unknown',
            'fitBasis':'lyric length/density plus explicit context only; no inferred emotion from book name',
            'passages':[{'startVerse':verses[i]['verse'],'endVerse':verses[min(len(verses)-1,i+max(1,len(verses)//3)-1)]['verse']}
                        for i in range(0,len(verses),max(1,len(verses)//3))] if verses else []}


def choose(identity,features,history):
    palette=read(Path(__file__).with_name('upbeat-variations.json'))
    available=[v for v in palette['variations'] if v['id'] in ELIGIBLE and
               v['status']=='tested' and v['lyric_mode']=='canonical_once']
    require({v['id'] for v in available}==set(ELIGIBLE),'DIRECTOR_PALETTE_INVALID')
    recent=history[-12:];last=recent[-1]['variationId'] if recent else None
    eligible=[v for v in available if v['id']!=last]
    counts={v['id']:sum(r['variationId']==v['id'] for r in recent) for v in eligible}
    weights={v['id']:1/(1+counts[v['id']]) for v in eligible}
    for r in recent[-2:]:
        if r['variationId'] in weights:weights[r['variationId']]*=0.5
    # Keep word-dense passages clear without pretending to infer chapter theology.
    if features['dense']:
        for key in ('guitar_solo','piano_solo'):
            if key in weights:weights[key]*=1.3
    total=sum(weights.values());prob={k:.25/len(weights)+.75*w/total for k,w in weights.items()}
    seed=secrets.randbits(64);rng=random.Random(seed)
    variation=rng.choices(list(prob),weights=list(prob.values()),k=1)[0]
    tempos=[100,104,108] if features['dense'] else [104,108,112,116]
    recent_tempos={r.get('tempoBpm') for r in recent[-2:]}
    tempo=rng.choice([t for t in tempos if t not in recent_tempos] or tempos)
    lead=rng.choice(['warm clear male tenor','warm clear female alto'])
    if recent and lead==recent[-1].get('lead'):
        lead='warm clear female alto' if 'male tenor' in lead else 'warm clear male tenor'
    styles={
      'guitar_solo':('rhythmic acoustic guitar','buoyant straight acoustic groove',
          f'Rhythmic acoustic guitar leads; piano accents, melodic bass, restrained kick/snare and shaker. One {lead} sings a memorable pitched melody.'),
      'piano_solo':('bright rhythmic piano','syncopated piano pop groove',
          f'Bright rhythmic piano leads a buoyant syncopated groove; acoustic guitar accents, melodic bass, light drums. One {lead} sings expressive pitched melodies.'),
      'alternating_duet':('acoustic guitar and piano','buoyant acoustic pop duet',
          f'Clear male tenor and female alto alternate whole passages, starting with the {"male" if "male tenor" in lead else "female"} voice. Acoustic guitar, rhythmic piano, buoyant bass and light drums. No echoed words; harmony only simultaneously.'),
      'suno_freedom':(None,'provider chosen within warm buoyant folk-pop',
          'Compose freely within warm buoyant devotional folk-pop: choose fresh melody, instruments, groove and clear singing voices. Use a recurring instrumental hook and clear section contrasts.')
    }
    instrument,groove,direction=styles[variation]
    vocals='alternating male/female duet' if variation=='alternating_duet' else 'provider chosen' if variation=='suno_freedom' else 'solo'
    if variation=='suno_freedom':lead=None
    fit=' Give dense scripture extra breathing space with lighter accompaniment.' if features['dense'] else ' Keep a bright forward pulse without rushing the words.'
    if features['context']=='lament':fit+=' Respect the solemn words; hopeful and restrained, never comic.'
    style=f'Upbeat devotional folk-pop, {tempo} BPM. '+direction+fit+' Fully pitched melodic singing, clear English diction. Brief intro, lighter middle, fuller ending; clean forward vocals.'
    return {'variationId':variation,'seed':seed,'selectionProbability':prob[variation],
        'eligibleProbabilities':prob,'recentPlanIds':[r['id'] for r in recent[-6:]],
        'policy':'25% uniform mixture, frequency/recency weights, exclude immediately previous treatment; no quality-learning claim',
        'planned':{'tempo_bpm':tempo,'lead':lead,'vocal_arrangement':vocals,
                   'primary_instrument':instrument,'groove':groove,'energy_arc':'brief opening, lighter middle, fuller resolved ending'},
        'style':style,'lyricsMode':'canonical_once'}


def prepare_request(job,request,recipe,lyrics,history_root=None):
    """Called under the existing job lock. Shared history lock serializes reservations."""
    job=Path(job);request_path=job/'request.json';plan_path=job/'director-plan.json'
    ident=input_identity(request,recipe,lyrics)
    if request_path.exists():
        # Legacy and paid/in-flight inputs are never replaced by a newly selected style.
        body=read(request_path)
        require(body.get('prompt')==lyrics and isinstance(body.get('tags'),str) and 0<len(body['tags'])<=1000,'GENERATION_INPUT_CHANGED')
        if plan_path.exists():
            plan=read(plan_path)
            require(plan.get('inputSha256')==ident and plan.get('requestSha256')==digest(canonical(body)),'DIRECTOR_INPUT_CHANGED')
        else:
            atomic(plan_path,{'version':VERSION,'mode':'legacy_preserved','inputSha256':ident,
                'requestSha256':digest(canonical(body)),'finalStylePrompt':body['tags'],'createdAt':utc(),
                'note':'Existing request reused byte-for-byte; no variation inferred or quality asserted.'})
        return body
    require(not (job/'studio').exists() and not (job/'studio-attempt.json').exists(),'DIRECTOR_MISSING_PAID_REQUEST_RECONCILE')
    if request['genre']!='upbeat':
        tags=recipe['style']+' '+recipe.get('lyrics_rules',RULES)
        require(len(tags)<=1000,'STYLE_TOO_LONG')
        body={'prompt':lyrics,'tags':tags};atomic(request_path,body);return body
    state=private(history_root or Path.home()/'.local/share/holysong-upbeat-director',True)
    with lock(state/'history.lock'):
        ledger_path=state/'reservations.json'
        ledger=read(ledger_path) if ledger_path.exists() else {'version':1,'entries':[]}
        require(ledger.get('version')==1 and isinstance(ledger.get('entries'),list),'DIRECTOR_HISTORY_INVALID')
        if plan_path.exists():
            plan=read(plan_path)
            require(plan.get('inputSha256')==ident and plan.get('mode')=='directed','DIRECTOR_INPUT_CHANGED')
        else:
            same=next((entry for entry in ledger['entries'] if entry.get('id')==ident),None)
            if same:
                # Same chapter/source under another account retains the same direction.
                plan=read(state/'plans'/(ident+'.json'))
            else:
                features=chapter_features(job,lyrics,request)
                selection=choose(ident,features,ledger['entries'])
                tags=selection['style']+' '+recipe.get('lyrics_rules',RULES)
                require(len(tags)<=1000,'DIRECTOR_STYLE_TOO_LONG')
                body={'prompt':lyrics,'tags':tags}
                plan={'id':ident,'version':VERSION,'mode':'directed','inputSha256':ident,
                    'createdAt':utc(),'chapter':{k:request.get(k) for k in ('book','chapter','genre','edition')},
                    'features':features,**selection,'finalStylePrompt':tags,
                    'requestSha256':digest(canonical(body)),'audio_verified_features':{},'human_review':'pending'}
                private(state/'plans',True);atomic(state/'plans'/(ident+'.json'),plan)
                ledger['entries'].append({'id':ident,'variationId':plan['variationId'],
                    'tempoBpm':plan['planned']['tempo_bpm'],'lead':plan['planned']['lead'],'createdAt':plan['createdAt']})
                atomic(ledger_path,ledger)
            atomic(plan_path,plan)
        # Recover a crash between writing a plan and request without selecting again.
        body={'prompt':lyrics,'tags':plan['finalStylePrompt']}
        require(digest(canonical(body))==plan['requestSha256'],'DIRECTOR_PLAN_CHANGED')
        atomic(request_path,body)
        return body
