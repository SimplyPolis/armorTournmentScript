from __future__ import annotations
import asyncio
import enum
from typing import Optional
import auraxium
from auraxium import event, ps2

import itertools
import datetime
import csv
import json
from quart import copy_current_websocket_context, Quart, websocket, request, jsonify, render_template, g
from logging import getLogger
from quart.logging import default_handler
from quart_auth import basic_auth_required, QuartAuth
import re
import secrets
import aiosqlite

app = Quart(__name__)
app.config["QUART_AUTH_BASIC_USERNAME"] = "hejbl"
app.config["QUART_AUTH_BASIC_PASSWORD"] = "out"
app.secret_key = secrets.token_urlsafe(16)

db = None

points = json.load(open('points.json'))
new_points = points.copy()
cached_response = None
game = None

initialize_request_re = re.compile(r"^team(?P<id>\d+)_(?P<field>.+)")
initialize_lock = asyncio.Lock()
initialize_cond = asyncio.Condition()
cache_lock = asyncio.Lock()
game_state_queue = asyncio.Queue()
game_task = None
table_name = None


class Faction(enum.IntEnum):
    VS = 1
    NC = 2
    TR = 3
    NS = 4


class Team:
    id_iter = itertools.count(start=0)

    def __init__(self, name: str, members: dict[int:str], faction: Faction):
        self.members = members
        self.score = 0
        self.name = name
        self.faction = faction
        self.id = next(Team.id_iter)

    def __eq__(self, other):
        if isinstance(other, Team):
            return self.id == other.id

    def __ne__(self, other):
        if isinstance(other, Team):
            return self.id != other.id

    def overlayAttributes(self):
        return {"name": self.name, "score": self.score, "faction": self.faction}

    def scorePoints(self, score: int):
        self.score += score

    def getPlayers(self) -> dict[int:int]:
        return {member: self.id for member in self.members.keys()}


class Game:
    def __init__(self, teams: list[(str, dict[int:str], Faction)]):
        self.teams = [Team(*team) for team in teams]
        self.players = {}
        self.names = {}
        for team in self.teams:
            self.names.update(team.members)
            self.players.update(team.getPlayers())

    def __repr__(self):
        return json.dumps({"teams": [team.overlayAttributes() for team in self.teams]})

    def __str__(self):
        return "\n".join([f"{team.name}: {team.score}" for team in self.teams])

    def findPlayer(self, id: int) -> Optional[Team]:
        if (player := self.players.get(id, None)) is None:
            return None
        return self.teams[player]

    @classmethod
    async def initializeTeams(cls, teams: list[(str, list[str], Faction)]) -> Game:
        teams_with_ids = []
        async with auraxium.Client(service_id='s:MEGABIGSTATS') as client:
            for team in teams:
                players = dict()
                for player in team[1]:
                    players.update({(await client.get_by_name(ps2.Character, player)).id: player})
                teams_with_ids.append((team[0], players, team[2]))
        return cls(teams_with_ids)


async def track_vehicle_events():
    global game_task
    global db
    game_task = asyncio.current_task()
    client = auraxium.event.EventClient(service_id='s:MEGABIGSTATS')
    async with initialize_lock:
        async with initialize_cond:
            while (not game):
                await initialize_cond.wait()
        print("Ready")
        print(game.players.keys())
        print(game.players.values())

        @client.trigger(event.VehicleDestroy, characters=game.players.keys())
        async def track_kill(evt: event.VehicleDestroy):

            victim_id = evt.character_id
            killer_id = evt.attacker_character_id
            killer_team = game.findPlayer(killer_id)
            victim_team = game.findPlayer(victim_id)
            killer_vehicle = evt.attacker_vehicle_id
            victim_vehicle = evt.vehicle_id

            # Ignore deaths not caused by enemy players

            if not victim_team or not killer_team or killer_id == 0 or victim_id == killer_id or points.get(
                    str(victim_vehicle), {"banned": True}) or points.get(str(killer_vehicle), {"banned": True}):
                pass
            elif killer_team != victim_team:
                killer_team.scorePoints(points[str(victim_vehicle)]["points"])
            elif killer_team == victim_team:
                killer_team.scorePoints(-(points[str(victim_vehicle)]["points"]))

            print("here")
            await game_state_queue.put(repr(game))
            await db.execute(
                f"""INSERT INTO {table_name} (timestamp,killer,victim,killer_vehicle,victim_vehicle,gamestate ) VALUES (?,?,?,?,?,?)""",
                [evt.timestamp.isoformat(),
                 game.names.get(killer_id, killer_id), game.names.get(victim_id, victim_id),
                 points.get(str(killer_vehicle), {"name": "None"})["name"],
                 points.get(str(victim_vehicle), {"name": "None"})["name"],
                 repr(game)])
            await db.commit()


async def input():
    while True:
        data = await websocket.receive()
        if data != "join":
            continue


async def on_vehicle_event():
    global cached_response
    while True:
        element = await game_state_queue.get()
        async with cache_lock:
            cached_response = element

        await websocket.send(element)


async def echo_cache():
    global cached_response
    while True:
        await asyncio.sleep(5)
        async with cache_lock:
            if not cached_response:
                continue
            await websocket.send(cached_response)


@app.websocket('/ws')
async def ws():
    on_vehicle_event_task = asyncio.ensure_future(
        copy_current_websocket_context(on_vehicle_event)(),
    )
    echo_cache_task = asyncio.ensure_future(
        copy_current_websocket_context(echo_cache)(),
    )
    try:
        await asyncio.gather(on_vehicle_event_task, echo_cache_task)
    finally:
        on_vehicle_event_task.cancel()
        echo_cache_task.cancel()


@app.route('/admin', methods=["GET", "POST"])
@basic_auth_required()
async def initializeGame():
    if request.method == "GET":
        return await render_template("admin.html")
    global game
    global table_name
    global db
    req_dict = await request.form
    teams_dict = {}
    for name, value in req_dict.lists():
        if (match := initialize_request_re.match(name)) is None:
            print(name)
            continue
        teams_dict.setdefault(match.group("id"), {}).update({match.group("field"): value})
    game_list = []
    team_nums = list(teams_dict.keys())
    team_nums.sort()

    for key in team_nums:
        game_list.append(
            (teams_dict[key]["name"][0], teams_dict[key]["players"], Faction(int(teams_dict[key]["faction"][0]))))
    async with initialize_lock:

        table_name = f"log_{str(datetime.datetime.now().timestamp()).replace('.', '')}"
        await db.execute(
            f"""CREATE TABLE {table_name} (
            timestamp DATETIME,
  			killer TEXT,
  			victim TEXT,
  			killer_vehicle NUMERIC,
			victim_vehicle NUMERIC,
  			gamestate TEXT)""")
        game = await Game.initializeTeams(game_list)
        async with initialize_cond:
            initialize_cond.notify()
    return jsonify(success=True)


@app.route('/startGame', methods=["POST"])
async def startGame():
    loop = asyncio.get_event_loop()

    await loop.create_task(track_vehicle_events())
    return jsonify(success=True)


@app.route('/endGame', methods=["POST"])
async def endGame():
    return jsonify(success=True) if endGameCallback() else jsonify(success=False)


def endGameCallback() -> bool:
    global game_task
    if not game_task:
        if not game_task:
            return False
        game_task.cancel()
        game_task = None
        return True


async def openDB():
    global db
    db = await aiosqlite.connect("game.db")


async def closeDB():
    global db
    await db.close()


if __name__ == '__main__':
    try:
        asyncio.run(openDB())
        app.run(host="localhost", port=8080)
    finally:
        asyncio.run(closeDB())
