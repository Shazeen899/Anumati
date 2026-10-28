import os
from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from core.models import *
from core.services import advance, audit, make_deadline, owner_for

class Command(BaseCommand):
    help = 'Create repeatable synthetic demonstration records. Never runs outside DEMO_MODE.'
    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEMO_MODE:
            raise CommandError('Enable DEMO_MODE explicitly before seeding synthetic records.')
        password = os.environ.get('DEMO_PASSWORD')
        if not password or len(password) < 12:
            raise CommandError('Set DEMO_PASSWORD to a local demonstration password of at least 12 characters.')
        institutions = []
        specs = [('Aryabhatta Institute of Technology','North','New Delhi'),('Saraswati College of Engineering','North','Jaipur'),('Vindhya Institute of Technology','Central','Bhopal'),('Sahyadri College of Engineering','West','Pune'),('Kaveri Technical Institute','South','Bengaluru'),('Brahmaputra Institute of Science','East','Guwahati')]
        for i,(name,region,city) in enumerate(specs):
            obj,_=Institution.objects.get_or_create(code=f'DEMO-INST-{i+1:03}',defaults={'name':name,'region':region,'city':city})
            institutions.append(obj)
        users={}
        roles=[('institution','institution','Aarav','Sharma','North',institutions[0]),('institution2','institution','Meera','Joshi','North',institutions[1]),('reviewer','reviewer','Priya','Mehta','',None),('evc','evc','Vikram','Rao','',None),('committee','committee','Kavita','Sen','',None),('regional','regional','Rohan','Verma','North',None),('bureau','bureau','Ananya','Rao','',None),('executive','executive','Dev','Kapoor','',None),('council','council','Nisha','Iyer','',None),('policy','policy','Ishaan','Gupta','',None),('auditor','auditor','Sara','Khan','',None),('operator','operator','Neel','Das','',None)]
        for username,role,first,last,region,inst in roles:
            u,created=User.objects.get_or_create(username=username,defaults={'first_name':first,'last_name':last,'email':username+'@example.test'})
            if created:
                u.set_password(password);u.save()
            Profile.objects.get_or_create(user=u,defaults={'role':role,'institution':inst,'region':region})
            users[username]=u
        for i,inst in enumerate(institutions[2:],3):
            u,created=User.objects.get_or_create(username=f'institution{i}',defaults={'first_name':f'Institution {i}','last_name':'Representative','email':f'institution{i}@example.test'})
            if created:
                u.set_password(password);u.save()
            Profile.objects.get_or_create(user=u,defaults={'role':'institution','institution':inst,'region':inst.region})
        routes={'new_institution':['SCRUTINY','EVC','DECISION','RATIFICATION'],'new_programme':['SCRUTINY','EVC','DECISION','RATIFICATION'],'name_change':['SCRUTINY','DECISION','RATIFICATION'],'closure':['SCRUTINY','DECISION','RATIFICATION'],'self_disclosure':['DECISION','RATIFICATION'],'evc_only':['EVC','DECISION','RATIFICATION']}
        policy,_=Policy.objects.get_or_create(name='Anumati demonstration routes',version=1,defaults={'state':'active','source':'Synthetic fixtures derived from the supplied PRD. Seven-day stage and three-day reminder offsets are demo assumptions, not certified APH rules. Production activation is disabled.','rules':{'routes':routes,'stage_days':7,'reminder_days':3},'approved_by':users['policy']})
        now=timezone.now()
        fixtures=[(0,'new_programme','SCRUTINY',-2),(1,'new_institution','EVC',1),(2,'name_change','SCRUTINY',3),(3,'evc_only','EVC',-1),(4,'new_programme','DECISION',4),(5,'name_change','RATIFICATION',2),(0,'name_change','APPROVED',0),(1,'new_programme','RETURNED',-3),(2,'closure','DECISION',5),(3,'name_change','APPROVED',0),(4,'self_disclosure','APPROVED',0),(0,'new_institution','DRAFT',7)]
        for i,(idx,category,stage,offset) in enumerate(fixtures,1):
            if Application.objects.filter(external_id=f'DEMO-{i:04}',source='Simulated intake').exists():
                continue
            c=Application.objects.create(reference=f'AN-2026-{i:04}',institution=institutions[idx],category=category,academic_year='2026-2027',external_id=f'DEMO-{i:04}',policy=policy,route=routes[category],payment_verified=stage!='DRAFT',created_at=now-timedelta(days=5+i),submitted_at=now-timedelta(days=4+i) if stage!='DRAFT' else None)
            c.stage_index=routes[category].index(stage) if stage in routes[category] else 0
            if stage=='DRAFT':
                c.stage='DRAFT';c.assigned_to=users['institution']
            elif stage=='RETURNED':
                advance(c,'SCRUTINY');c.stage='RETURNED';c.assigned_to=owner_for(c,'RETURNED');c.blocker='Please provide the revised laboratory inventory against the approved intake.'
                Deficiency.objects.create(application=c,finding=c.blocker,requirement='DEMO-LAB-01: laboratory inventory')
            elif stage=='APPROVED':
                for j,s in enumerate(routes[category]):
                    StageRecord.objects.create(application=c,stage=s,entered_at=now-timedelta(days=9-j),completed_at=now-timedelta(days=8-j),finding='Synthetic demonstration stage completion.')
                c.stage=stage;c.assigned_to=users['council'];c.published_at=now-timedelta(days=2)
                Decision.objects.create(application=c,kind='executive',outcome='approved',reason='Synthetic approval fixture for the compliance demonstration.',actor=users['executive'],case_version=2)
                decision=Decision.objects.create(application=c,kind='ratification',outcome='approved',reason='Synthetic Council ratification.',actor=users['council'],case_version=3)
                c.version=3
                for title,kind,days in [('Submit infrastructure undertaking','undertaking',3),('Registered lease document','lease',-4)]:
                    o=Obligation.objects.create(application=c,decision=decision,title=title,kind=kind,source='Synthetic continuing obligation; domain validation required.',verifier=users['reviewer'])
                    make_deadline(c,title,'submission',now+timedelta(days=days),owner_for(c,'RETURNED'),o)
            else:
                for j,s in enumerate(routes[category][:c.stage_index]):
                    StageRecord.objects.create(application=c,stage=s,entered_at=now-timedelta(days=10-j),completed_at=now-timedelta(days=9-j),finding='Synthetic prior-stage completion.')
                advance(c,stage)
                if stage=='RATIFICATION':
                    Decision.objects.create(application=c,kind='executive',outcome='approved',reason='Synthetic Executive Committee recommendation package reviewed.',actor=users['executive'],case_version=1)
            c.save()
            c.deadlines.filter(kind='stage').update(due_at=now+timedelta(days=offset))
            if stage=='EVC' and idx==1:
                EVCVisit.objects.create(application=c,coordinator=users['evc'],starts_at=now-timedelta(hours=2),ends_at=now-timedelta(hours=1),status='confirmed')
            audit(c,users['bureau'],'demo_intake','Synthetic case created for a repeatable demonstration.',details={'synthetic':True})
        self.stdout.write(self.style.SUCCESS('Synthetic accounts and cases are ready. Existing passwords and records were preserved.'))
