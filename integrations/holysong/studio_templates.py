"""Account-free Studio wire schema, version 1. No captured HAR or project data."""
from uuid import uuid4

VERSION = 'studio-clean-v1'

def render_template():
    clip = dict(type='audio', streaming=False, id=str(uuid4()), asset={'type':'clip','id':str(uuid4())},
                mute=False, reversed=False, name='', color='#208BFF', startBeats=0, endBeats=0,
                readStartBeats=0, fadeInBeats=0, fadeOutBeats=0, fadeInCurve=1, fadeOutCurve=1,
                transposition=0, formantCorrection=0, amplitude=1,
                loop={'enabled':False,'startBeats':0,'endBeats':0},
                warp={'enabled':False,'awaitingAnalysis':False}, generatedInPlace=False,
                awaitingContentAlignment=False, clipId=None, uploadId=None)
    track = dict(type='audio',id=str(uuid4()),input=None,color='#208BFF',name='',height=86,
                 solo=False,mute=False,arm=False,amplitude=1,balance=0,instrument={'type':'song'},
                 soloTakeLaneId=None,takeLanesExpanded=False,hidden=False,routingMode='linear',
                 faderAutomation={},clips=[clip],clipCreationIntents=[],signalChain=[],takeLanes=[])
    eq = {'enabled':False}
    for i,(kind,freq,enabled) in enumerate([('highpass',60,False),('lowshelf',160,True),
        ('peaking',450,True),('peaking',1200,True),('highshelf',3200,True),('lowpass',8800,False)],1):
        eq['band'+str(i)]={'type':kind,'enabled':enabled,'frequency':freq,'gain':0,'q':0.707}
    track['eq']=eq
    state = dict(amplitude=1,sections=[],lyricsCorrectionsByClipId={},metronome={'enabled':False,'amplitude':1},
        timing={'type':'manual','bps':2,'bpsAutomation':[],'firstBeatSeconds':0},
        timeSignatureChanges=[{'startBeats':0,'subdivisionsPerBar':4,'beatsPerSubdivision':1}],
        actors={},loop={'enabled':False,'startBeats':0,'endBeats':0},songFadeInBeats=0,songFadeOutBeats=0,
        masterRoutingMode='linear',routing={},recording={'countInBars':0,'preRollBars':0},
        midiCcMappings={},majorVersion=2,limiterLookaheadMs=0,midiNotePreviewEnabled=True,
        takePreviews={},masterSignalChain=[],midiControllerSignalChain=[],tracks=[track],markersRegistry={})
    return dict(title='',lyrics='',tags='',negative_tags='',style_summary='',caption='',state=state,
                start_beats=0,end_beats=0,project_id=None,studio_project_id=None,
                studio_project_version_id=None,web_client_pathname='/studio',downbeats=[],
                export_mode='fullsong_export',render_engine='v2')

def generation_template(model='chirp-hawk'):
    return dict(token=None,task='agentic_instant',generation_type='TEXT',mv=model,prompt='',
        gpt_description_prompt='',make_instrumental=False,user_uploaded_images_b64=None,
        metadata=dict(web_client_pathname='/studio',create_surface='studio',create_mode='simple',
                      disable_volume_normalization=False,lyrics_model='default'),
        override_fields=[],cover_clip_id=None,cover_start_s=None,cover_end_s=None,persona_id=None,
        artist_clip_id=None,artist_start_s=None,artist_end_s=None,continue_clip_id=None,
        continued_aligned_prompt=None,continue_at=None,transaction_uuid=None,token_provider=2,
        attached_lyrics='',attached_styles='')
