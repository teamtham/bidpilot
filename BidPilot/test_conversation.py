import base64
import os
from pathlib import Path
import tempfile
import unittest
import db
import conversation as chat

class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=os.path.join(self.tmp.name,'test.db'); self.c=db.connect(self.path); self.c.executescript(chat.SCHEMA)
        self.created=chat.create_inquiry(self.c); self.i=self.created['inquiry_id']
    def tearDown(self): self.c.close(); self.tmp.cleanup()
    def test_atomic_retry_and_stale(self):
        m={'text':'Two reels','request_id':'m1'}
        self.assertEqual(chat.add_message(self.c,self.i,m),chat.add_message(self.c,self.i,m))
        with self.assertRaisesRegex(ValueError,'request_id_conflict'): chat.add_message(self.c,self.i,{'text':'changed','request_id':'m1'})
        self.assertEqual(len(chat.pending(self.c)),1)
        args={'inquiry_id':self.i,'through_sequence':1,'expected_revision':0,'request_id':'r1','reply':'Thanks','fact_changes':[{'subject':'videos','value':2,'status':'confirmed','source_sequence':1}]}
        result=chat.commit(self.c,args); self.assertEqual(result,chat.commit(self.c,args)); self.assertEqual(result['revision'],1)
        chat.add_message(self.c,self.i,{'text':'Actually three','request_id':'m2'})
        with self.assertRaisesRegex(ValueError,'stale_revision'): chat.commit(self.c,{**args,'request_id':'r2','expected_revision':1})
        self.assertEqual(len(chat.context(self.c,{'inquiry_id':self.i})['turns']),3)
    def test_cross_inquiry_references_are_rejected(self):
        other=chat.create_inquiry(self.c)['inquiry_id']
        asset=chat.upload(self.c,self.i,{'filename':'fake.mp4','mime':'video/mp4','data':base64.b64encode(b'invalid').decode()})['asset_id']
        with self.assertRaisesRegex(ValueError,'invalid_references'): chat.add_message(self.c,other,{'text':'a','request_id':'x','references':[{'asset_id':asset}]})
        self.assertTrue(chat.process_media_once(self.path))
        self.assertEqual(self.c.execute('select status from reference_runs').fetchone()[0],'failed')
    def test_deal_binding_requires_matching_site_identity(self):
        for did, source, sid in [('good','site','inquiry-'+self.i),('wrong','site','inquiry-other'),('old','inquiry',self.i)]:
            self.c.execute("INSERT INTO deals (id,source,source_id,client,raw_inquiry,status) VALUES (?,?,?,?,?,?)",(did,source,sid,'Client','Brief','new'))
        self.c.commit()
        for did in ('wrong','old'):
            with self.assertRaisesRegex(ValueError,'invalid_deal_source'): chat.bind_deal(self.c,{'inquiry_id':self.i,'deal_id':did})
        self.assertEqual(chat.bind_deal(self.c,{'inquiry_id':self.i,'deal_id':'good'})['deal_id'],'good')

    def test_media_completion_wakes_and_acknowledges(self):
        asset=chat.upload(self.c,self.i,{'filename':'bad.mp4','mime':'video/mp4','data':base64.b64encode(b'bad').decode()})
        chat.process_media_once(self.path)
        item=chat.pending(self.c)[0]
        self.assertEqual(item['media_updates'][0]['analysis_id'],asset['analysis_id'])
        self.assertEqual(item['media_updates'][0]['status'],'failed')
        self.assertEqual(chat.context(self.c,{'inquiry_id':self.i})['references'][0]['status'],'failed')
        with self.assertRaisesRegex(ValueError,'not_found'): chat.acknowledge_media(self.c,{'inquiry_id':'other','analysis_id':asset['analysis_id']})
        chat.acknowledge_media(self.c,{'inquiry_id':self.i,'analysis_id':asset['analysis_id']})
        self.assertEqual(chat.pending(self.c),[])

    def test_reply_guards(self):
        chat.add_message(self.c,self.i,{'text':'Two videos, budget $500','request_id':'m'})
        args={'inquiry_id':self.i,'through_sequence':1,'expected_revision':0,'request_id':'r','reply':'Your requested budget is $500','fact_changes':[]}
        for reply in ('Our price floor is $500', 'We can do it for $900', 'We can do it for $500'):
            with self.assertRaises(ValueError): chat.commit(self.c,{**args,'reply':reply})
        chat.commit(self.c,{**args,'reply':'Would a 15-second cut or 30-second cut suit you?'})

    def test_media_update_can_reply_and_persist_grounded_agent_cue(self):
        chat.add_message(self.c,self.i,{'text':'Use this reference','request_id':'m'})
        chat.commit(self.c,{'inquiry_id':self.i,'through_sequence':1,'expected_revision':0,
            'request_id':'first','reply':'I will review the reference.','fact_changes':[]})
        asset=chat.upload(self.c,self.i,{'filename':'bad.mp4','mime':'video/mp4',
            'data':base64.b64encode(b'bad').decode()})
        chat.process_media_once(self.path)
        # Failed media still permits a scoped follow-up without a newer buyer turn.
        result=chat.commit(self.c,{'inquiry_id':self.i,'through_sequence':1,
            'expected_revision':0,'request_id':'media-failed','reply':'I could not decode that reference.',
            'fact_changes':[],'media_analysis_id':asset['analysis_id'],'production_cues':[]})
        self.assertEqual(result,chat.commit(self.c,{'inquiry_id':self.i,'through_sequence':1,
            'expected_revision':0,'request_id':'media-failed','reply':'I could not decode that reference.',
            'fact_changes':[],'media_analysis_id':asset['analysis_id'],'production_cues':[]}))
        self.assertEqual(chat.pending(self.c),[])

        # Insert deterministic grounded evidence to exercise cue provenance.
        run='analysis-grounded'; aid='asset-grounded'; frame_dir=Path(self.path+'.media')/run
        frame_dir.mkdir(parents=True); (frame_dir/'frame-1.jpg').write_bytes(b'jpeg')
        self.c.execute('INSERT INTO reference_assets VALUES (?,?,?,?,?,?,?)',(aid,self.i,'r.mp4','video/mp4','d',str(Path(self.path+'.media')/'r.video'),1))
        self.c.execute('INSERT INTO reference_runs(id,asset_id,status,created_at) VALUES (?,?,?,?)',(run,aid,'done',1))
        obs='observation-1'; self.c.execute('INSERT INTO reference_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',(obs,run,2,3,'frame','Representative frame','FFmpeg sampled frame','Sample only','frame-1.jpg',None,1))
        self.c.execute('INSERT INTO media_notifications VALUES (?,?,?)',(run,self.i,'queued')); self.c.commit()
        chat.commit(self.c,{'inquiry_id':self.i,'through_sequence':1,'expected_revision':0,
            'request_id':'media-cue','reply':'The pacing reference is noted.','fact_changes':[],
            'media_analysis_id':run,'production_cues':[{'observation_id':obs,
                'body':'Use a restrained opening pace','uncertainty':'Creative interpretation'}]})
        cue=next(o for o in chat.evidence(self.c,{'analysis_id':run})['observations'] if o['kind']=='production_cue')
        self.assertEqual(cue['method'],'QwenWork agent cue'); self.assertEqual(cue['frame'],'frame-1.jpg')
        self.assertEqual(cue['supersedes'],obs)

    def test_claim_release_and_expiry(self):
        chat.add_message(self.c,self.i,{'text':'Hello','request_id':'m'})
        first=chat.claim_work(self.c,{'inquiry_id':self.i,'worker_id':'agent-a','lease_seconds':60})
        self.assertEqual(first['through_sequence'],1)
        # A live lease hides the work from other consumers and blocks claims.
        self.assertEqual(chat.pending(self.c),[])
        with self.assertRaisesRegex(ValueError,'already_leased'):
            chat.claim_work(self.c,{'inquiry_id':self.i,'worker_id':'agent-b'})
        # The owning worker can refresh its own lease.
        self.assertEqual(chat.claim_work(self.c,{'inquiry_id':self.i,'worker_id':'agent-a'})['through_sequence'],1)
        self.assertEqual(chat.release_work(self.c,{'inquiry_id':self.i,'worker_id':'agent-a'})['released'],1)
        self.assertEqual(len(chat.pending(self.c)),1)
        chat.claim_work(self.c,{'inquiry_id':self.i,'worker_id':'agent-b'})
        # An expired lease is reclaimable by another worker.
        self.c.execute("UPDATE inquiry_jobs SET lease_until=0 WHERE inquiry_id=?",(self.i,)); self.c.commit()
        chat.claim_work(self.c,{'inquiry_id':self.i,'worker_id':'agent-a'})
        chat.commit(self.c,{'inquiry_id':self.i,'through_sequence':1,'expected_revision':0,
                            'request_id':'r','reply':'Thanks for reaching out.','fact_changes':[]})
        row=self.c.execute("SELECT status,lease_until,lease_owner FROM inquiry_jobs WHERE inquiry_id=?",(self.i,)).fetchone()
        self.assertEqual(tuple(row),('done',None,None))
        self.assertIsNone(chat.claim_work(self.c,{'inquiry_id':self.i,'worker_id':'agent-a'})['through_sequence'])

    def test_sample_portfolio_clip(self):
        path=Path(__file__).parent/'sample-portfolio'/'cafe.mp4'
        asset=chat.upload(self.c,self.i,{'filename':path.name,'mime':'video/mp4','data':base64.b64encode(path.read_bytes()).decode()})
        chat.process_media_once(self.path)
        result=chat.evidence(self.c,{'analysis_id':asset['analysis_id']})
        self.assertEqual(result['status'],'done',result)
        self.assertTrue(any(o['kind']=='frame' for o in result['observations']))
        self.assertGreater(result['result']['duration'],0)
        # The synthetic sample has no audio stream; that is a distinct,
        # non-failure outcome rather than a broken transcription adapter.
        if chat.ffmpeg_executable():
            self.assertEqual(result['result']['transcription']['status'],'no_audio',result['result'])

    def test_audio_clip_transcribes(self):
        import shutil, subprocess, sys
        ffmpeg=chat.ffmpeg_executable()
        if not ffmpeg: self.skipTest('ffmpeg unavailable')
        prefix=Path(sys.prefix)
        whisper=os.environ.get('BIDPILOT_WHISPER_CLI') or (str(prefix/'bin'/'whisper-cli') if (prefix/'bin'/'whisper-cli').is_file() else shutil.which('whisper-cli'))
        model=os.environ.get('BIDPILOT_WHISPER_MODEL') or (str(prefix/'share'/'bidpilot'/'ggml-tiny.en.bin') if (prefix/'share'/'bidpilot'/'ggml-tiny.en.bin').is_file() else None)
        if not whisper or not model:
            venv=Path(__file__).parent.parent/'.venv'
            whisper=str(venv/'bin'/'whisper-cli') if (venv/'bin'/'whisper-cli').is_file() else None
            model=str(venv/'share'/'bidpilot'/'ggml-tiny.en.bin') if (venv/'share'/'bidpilot'/'ggml-tiny.en.bin').is_file() else None
        if not whisper or not model: self.skipTest('whisper adapter unavailable')
        clip=Path(self.tmp.name)/'tone.mp4'
        subprocess.run([ffmpeg,'-y','-f','lavfi','-i','testsrc=duration=3:size=160x120:rate=10',
                        '-f','lavfi','-i','sine=frequency=440:duration=3',
                        '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(clip)],
                       capture_output=True,timeout=60,check=True)
        os.environ['BIDPILOT_WHISPER_CLI']=whisper; os.environ['BIDPILOT_WHISPER_MODEL']=model
        try:
            asset=chat.upload(self.c,self.i,{'filename':'tone.mp4','mime':'video/mp4','data':base64.b64encode(clip.read_bytes()).decode()})
            chat.process_media_once(self.path)
        finally:
            os.environ.pop('BIDPILOT_WHISPER_CLI',None); os.environ.pop('BIDPILOT_WHISPER_MODEL',None)
        result=chat.evidence(self.c,{'analysis_id':asset['analysis_id']})
        self.assertEqual(result['status'],'done',result)
        self.assertEqual(result['result']['transcription']['status'],'complete',result['result'])
        self.assertEqual(result['result']['transcription']['provider'],'whisper.cpp')

    def test_real_video_decode(self):
        path=Path('/System/Library/CoreServices/Setup Assistant.app/Contents/Resources/trackpad_placeholder.mov')
        if not path.exists() or path.stat().st_size>chat.MAX_UPLOAD: self.skipTest('macOS sample video unavailable')
        asset=chat.upload(self.c,self.i,{'filename':'sample.mov','mime':'video/quicktime','data':base64.b64encode(path.read_bytes()).decode()})
        self.assertTrue(chat.process_media_once(self.path))
        evidence=chat.evidence(self.c,{'analysis_id':asset['analysis_id']})
        self.assertEqual(evidence['status'],'done', evidence)
        frame=next(o for o in evidence['observations'] if o['kind']=='frame')
        self.assertTrue(base64.b64decode(chat.reference_frame(self.c,{'observation_id':frame['id']})['data_base64']).startswith(b'\xff\xd8'))

    def test_c2pa_manifest_clip_decodes(self):
        # AI-export and content-authenticity tools embed a top-level C2PA uuid
        # box before moov. AVFoundation rejected the live coffee.mp4 upload
        # (-11828) while ffmpeg decoded it; the fixture re-embeds that upload's
        # real manifest box so the ffmpeg path must keep handling such files.
        ffmpeg=chat.ffmpeg_executable()
        if not ffmpeg: self.skipTest('ffmpeg unavailable')
        import subprocess
        clip=Path(self.tmp.name)/'plain.mp4'
        subprocess.run([ffmpeg,'-y','-f','lavfi','-i','testsrc=duration=3:size=160x120:rate=10',
                        '-c:v','libx264','-pix_fmt','yuv420p',str(clip)],
                       capture_output=True,timeout=60,check=True)
        raw=clip.read_bytes()
        box=(Path(__file__).parent/'test-fixtures'/'c2pa-uuid-box.bin').read_bytes()
        ftyp_size=int.from_bytes(raw[0:4],'big')
        patched=bytearray(raw[:ftyp_size]+box+raw[ftyp_size:])
        # inserting before mdat shifts absolute chunk offsets in stco/co64
        moov=patched.find(b'moov')-4
        for tag,width in ((b'stco',4),(b'co64',8)):
            pos=moov
            while True:
                i=patched.find(tag,pos)
                if i<0: break
                size=int.from_bytes(patched[i-4:i],'big')
                count=int.from_bytes(patched[i+8:i+12],'big')
                for k in range(count):
                    off=i+12+width*k
                    patched[off:off+width]=(int.from_bytes(patched[off:off+width],'big')+len(box)).to_bytes(width,'big')
                pos=i+size
        asset=chat.upload(self.c,self.i,{'filename':'c2pa.mp4','mime':'video/mp4','data':base64.b64encode(bytes(patched)).decode()})
        self.assertTrue(chat.process_media_once(self.path))
        result=chat.evidence(self.c,{'analysis_id':asset['analysis_id']})
        self.assertEqual(result['status'],'done',result)
        self.assertTrue(any(o['kind']=='frame' for o in result['observations']))
        self.assertGreater(result['result']['duration'],0)

    def test_client_removes_uploaded_reference(self):
        asset=chat.upload(self.c,self.i,{'filename':'oops.mp4','mime':'video/mp4','data':base64.b64encode(b'not-a-video').decode()})
        aid=asset['asset_id']
        path=Path(self.c.execute('SELECT path FROM reference_assets WHERE id=?',(aid,)).fetchone()[0])
        self.assertTrue(path.is_file())
        self.assertEqual([a['id'] for a in chat.context(self.c,{'inquiry_id':self.i})['references']],[aid])
        # Another inquiry cannot remove this client's reference.
        other=chat.create_inquiry(self.c)['inquiry_id']
        with self.assertRaisesRegex(ValueError,'not_found'): chat.remove_reference(self.c,other,{'asset_id':aid})
        self.assertEqual(chat.remove_reference(self.c,self.i,{'asset_id':aid}),{'removed':aid})
        self.assertFalse(path.exists())
        self.assertEqual(chat.context(self.c,{'inquiry_id':self.i})['references'],[])
        self.assertIsNone(self.c.execute('SELECT 1 FROM reference_runs WHERE asset_id=?',(aid,)).fetchone())
        # A removed reference can no longer be cited in a new message.
        with self.assertRaisesRegex(ValueError,'invalid_references'): chat.add_message(self.c,self.i,{'text':'use it','request_id':'m','references':[{'asset_id':aid}]})
        with self.assertRaisesRegex(ValueError,'not_found'): chat.remove_reference(self.c,self.i,{'asset_id':aid})
        with self.assertRaisesRegex(ValueError,'invalid_reference'): chat.remove_reference(self.c,self.i,{'asset_id':''})

    def test_remove_reference_clears_evidence_and_media_queue(self):
        run='analysis-x'; aid='asset-x'
        frame_dir=Path(self.path+'.media')/run; frame_dir.mkdir(parents=True); (frame_dir/'frame-1.jpg').write_bytes(b'jpeg')
        video=Path(self.path+'.media')/'x.video'; video.write_bytes(b'vid')
        self.c.execute('INSERT INTO reference_assets VALUES (?,?,?,?,?,?,?)',(aid,self.i,'r.mp4','video/mp4','d',str(video),1))
        self.c.execute('INSERT INTO reference_runs(id,asset_id,status,created_at) VALUES (?,?,?,?)',(run,aid,'done',1))
        self.c.execute('INSERT INTO reference_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',('obs-x',run,0,1,'frame','Frame','FFmpeg sampled frame','Sample only','frame-1.jpg',None,1))
        self.c.execute('INSERT INTO media_notifications VALUES (?,?,?)',(run,self.i,'queued')); self.c.commit()
        self.assertTrue(any('media_updates' in it for it in chat.pending(self.c)))
        chat.remove_reference(self.c,self.i,{'asset_id':aid})
        self.assertFalse(video.exists()); self.assertFalse(frame_dir.exists())
        self.assertIsNone(self.c.execute('SELECT 1 FROM reference_observations WHERE run_id=?',(run,)).fetchone())
        self.assertIsNone(self.c.execute('SELECT 1 FROM media_notifications WHERE run_id=?',(run,)).fetchone())
        self.assertEqual(chat.pending(self.c),[])

if __name__=='__main__': unittest.main()
