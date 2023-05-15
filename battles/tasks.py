import csv
import json
import zipfile
from datetime import datetime, timezone, timedelta
from io import StringIO, BytesIO
from urllib.parse import urljoin

import boto3
from anymail.message import AnymailMessage
from celery import shared_task
from django.conf import settings
from django.utils.crypto import salted_hmac

from battles.data_exports import sign_url
from battles.models import Battle, Player, PlayerGear, BattleAward
from splatnet_assets.models import Award
from users.models import User


def get_boto3_client():
    return boto3.client(
        service_name='s3',
        endpoint_url=settings.B2_ENDPOINT_URL,
        aws_access_key_id=settings.B2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.B2_SECRET_ACCESS_KEY,
    )


SALT = 'battle-export'


def hash_id(id_to_hash):
    return salted_hmac(SALT, str(id_to_hash)).hexdigest()


battle_fields = [
    'battle_id',
    'uploader_id',
    'has_disconnected_players',
    'vs_mode',
    'vs_rule',
    'stage_id',
    'played_time',
    'judgement',
    'knockout',
    'award_1',
    'award_2',
    'award_3',
]

team_fields = [
    'battle_id',
    'team_id',
    'team_color',
    'is_my_team',
    'judgement',
    'score',
    'paint_ratio',
    'noroshi',
    'fest_streak_win_count',
    'fest_team_name',
    'fest_uniform_bonus_rate',
    'fest_uniform_name',
    'tricolor_role',
]

player_fields = [
    'battle_id',
    'team_id',
    'player_id',
    'npln_id',
    'is_self',
    'weapon_id',
    'species',
    'disconnected',
    'kills',
    'assists',
    'deaths',
    'specials',
    'paint',
    'noroshi_try'
]


@shared_task
def update_global_battle_data():
    included_battles = Battle.objects.with_prefetch().exclude(
        vs_mode='PRIVATE',
    )

    battle_file = StringIO()
    battle_data = csv.DictWriter(battle_file, fieldnames=battle_fields)
    battle_data.writeheader()

    teams_file = StringIO()
    teams_data = csv.DictWriter(teams_file, fieldnames=team_fields)
    teams_data.writeheader()

    players_file = StringIO()
    players_data = csv.DictWriter(players_file, fieldnames=player_fields)
    players_data.writeheader()

    for battle in included_battles:
        award_1 = Award.objects.filter(battleaward__battle=battle, battleaward__order=0).first()
        award_1_id = award_1.id if award_1 else None

        award_2 = Award.objects.filter(battleaward__battle=battle, battleaward__order=1).first()
        award_2_id = award_2.id if award_2 else None

        award_3 = Award.objects.filter(battleaward__battle=battle, battleaward__order=2).first()
        award_3_id = award_3.id if award_3 else None

        battle_data.writerow({
            'battle_id': hash_id(battle.id),
            'uploader_id': hash_id(battle.uploader_id),
            'has_disconnected_players': Player.objects.filter(team__battle=battle, disconnect=True).exists(),
            'vs_mode': battle.vs_mode,
            'vs_rule': battle.vs_rule,
            'stage_id': battle.vs_stage_id,
            'played_time': battle.played_time,
            'judgement': battle.judgement,
            'knockout': battle.knockout,
            'award_1': award_1_id,
            'award_2': award_2_id,
            'award_3': award_3_id,
        })

        for team in battle.teams.all():
            teams_data.writerow({
                'battle_id': hash_id(battle.id),
                'team_id': hash_id(team.id),
                'team_color': team.color.to_hex(),
                'is_my_team': team.is_my_team,
                'judgement': team.judgement,
                'score': team.score,
                'paint_ratio': team.paint_ratio,
                'noroshi': team.noroshi,
                'fest_streak_win_count': team.fest_streak_win_count,
                'fest_team_name': team.fest_team_name,
                'fest_uniform_bonus_rate': team.fest_uniform_bonus_rate,
                'fest_uniform_name': team.fest_uniform_name,
                'tricolor_role': team.tricolor_role,
            })

            for player in team.players.all():
                players_data.writerow({
                    'battle_id': hash_id(battle.id),
                    'team_id': hash_id(team.id),
                    'player_id': hash_id(player.id),
                    'npln_id': hash_id(player.npln_id),
                    'is_self': player.is_self,
                    'species': player.species,
                    'weapon_id': player.weapon.internal_id,
                    'kills': player.kills,
                    'assists': player.assists,
                    'deaths': player.deaths,
                    'specials': player.specials,
                    'paint': player.paint,
                    'noroshi_try': player.noroshi_try,
                })

    # upload files to Backblaze B2
    client = get_boto3_client()

    current_date = datetime.now()

    zip_file_path = f'global/{current_date.year}/{current_date.month}/{current_date.day}/{current_date.hour}.zip'

    # zip up all three files together
    zip_file = BytesIO()
    with zipfile.ZipFile(zip_file, 'w') as zf:
        zf.writestr('battles.csv', battle_file.getvalue())
        zf.writestr('teams.csv', teams_file.getvalue())
        zf.writestr('players.csv', players_file.getvalue())

    zip_file.seek(0)
    client.upload_fileobj(zip_file, 'splashcat-data-exports', zip_file_path)


@shared_task
def user_request_data_export(user: User | int):
    if isinstance(user, int):
        user: User = User.objects.get(pk=user)

    battles = user.battles.with_prefetch().order_by('-played_time')

    battle_array = []

    battle: Battle
    for battle in battles:
        battle_data = {
            'battle_id': battle.id,
            'uploader_id': battle.uploader_id,
            'data_type': battle.data_type,
            'raw_data': battle.raw_data,
            'vs_mode': battle.vs_mode,
            'vs_rule': battle.vs_rule,
            'stage_id': battle.vs_stage_id,
            'uploaded_at': battle.uploaded_at.isoformat(),
            'updated_at': battle.updated_at.isoformat(),
            'played_time': battle.played_time.isoformat(),
            'duration': battle.duration.total_seconds(),
            'judgement': battle.judgement,
            'knockout': battle.knockout,
            'awards': [],
            'teams': [],
        }

        for award in battle.awards.all():
            battle_data['awards'].append({
                'id': award.id,
                'name': award.name.string,
                'order': BattleAward.objects.get(battle=battle, award=award).order,
            })

        for team in battle.teams.all():
            team_data = {
                'is_my_team': team.is_my_team,
                'color': team.color.to_hex(),
                'fest_streak_win_count': team.fest_streak_win_count,
                'fest_team_name': team.fest_team_name,
                'fest_uniform_bonus_rate': team.fest_uniform_bonus_rate,
                'fest_uniform_name': team.fest_uniform_name,
                'tricolor_role': team.tricolor_role,
                'judgement': team.judgement,
                'score': team.score,
                'paint_ratio': team.paint_ratio,
                'noroshi': team.noroshi,
                'order': team.order,
                'players': [],
            }

            player: Player
            for player in team.players.all():
                player_data = {
                    'is_self': player.is_self,
                    'species': player.species,
                    'npln_id': player.npln_id,
                    'name': player.name,
                    'name_id': player.name_id,
                    'title': player.byname,
                    'title_adjective_id': player.title_adjective.internal_id,
                    'title_subject_id': player.title_subject.internal_id,
                    'nameplate_background_id': player.nameplate_background.internal_id,
                    'nameplate_badge_ids': [
                        player.nameplate_badge_1.internal_id if player.nameplate_badge_1 else None,
                        player.nameplate_badge_2.internal_id if player.nameplate_badge_2 else None,
                        player.nameplate_badge_3.internal_id if player.nameplate_badge_3 else None,
                    ],
                    'weapon_id': player.weapon.internal_id,
                    'head_gear': {},
                    'clothing_gear': {},
                    'shoes_gear': {},
                    'disconnect': player.disconnect,
                    'kills': player.kills,
                    'assists': player.assists,
                    'deaths': player.deaths,
                    'specials': player.specials,
                    'paint': player.paint,
                    'noroshi_try': player.noroshi_try,
                    'order': player.order,
                }

                def serialize_player_gear(gear: PlayerGear):
                    return {
                        'gear_id': gear.gear.internal_id,
                        'primary_ability_id': gear.primary_ability.internal_id,
                        'secondary_ability_ids': [
                            gear.secondary_abilities[0].internal_id if gear.secondary_abilities[0] else None,
                            gear.secondary_abilities[1].internal_id if gear.secondary_abilities[1] else None,
                            gear.secondary_abilities[2].internal_id if gear.secondary_abilities[2] else None,
                        ],
                    }

                player_data['head_gear'] = serialize_player_gear(player.head_gear)
                player_data['clothing_gear'] = serialize_player_gear(player.clothing_gear)
                player_data['shoes_gear'] = serialize_player_gear(player.shoes_gear)

                team_data['players'].append(player_data)

            battle_data['teams'].append(team_data)

        battle_array.append(battle_data)

    json_data = json.dumps(battle_array, indent=4, ensure_ascii=False)

    client = get_boto3_client()

    temp_file = StringIO(json_data)
    temp_file = BytesIO(temp_file.read().encode('utf-8'))

    temp_file.seek(0)
    current_date = datetime.now().isoformat()
    client.upload_fileobj(temp_file, 'splashcat-data-exports', f'user/{user.id}-{current_date}.json')

    # email the user a link to download

    url = urljoin(f'https://{settings.BUNNY_NET_DATA_EXPORTS_CDN_HOST}', f'user/{user.id}-{current_date}.json')
    url = sign_url(url, expiration_time=timedelta(days=7))

    message = AnymailMessage(
        subject='Your data export is ready',
        body=f'Your data export is ready. You can download it at <meow this is temp, add url later>',
        to=[user.email],
    )

    message.esp_extra = {
        'MessageStream': 'data-export-finished',
    }

    message.send()


@shared_task
def cleanup_old_exports():
    # delete all global exports that are >48 hours old
    # delete all user exports that are >1 week old
    client = get_boto3_client()

    paginator = client.get_paginator('list_objects_v2')
    page_iterator = paginator.paginate(
        Bucket='splashcat-data-exports',
        Prefix='global/',
    )

    for page in page_iterator:
        for item in page['Contents']:
            if item['LastModified'] < datetime.now(timezone.utc) - timedelta(days=2):
                client.delete_object(
                    Bucket='splashcat-data-exports',
                    Key=item['Key'],
                )

    page_iterator = paginator.paginate(
        Bucket='splashcat-data-exports',
        Prefix='user/',
    )

    for page in page_iterator:
        for item in page['Contents']:
            if item['LastModified'] < datetime.now(timezone.utc) - timedelta(days=7):
                client.delete_object(
                    Bucket='splashcat-data-exports',
                    Key=item['Key'],
                )

    return True