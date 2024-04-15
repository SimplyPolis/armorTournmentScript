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
from quart import copy_current_websocket_context, Quart, websocket, request, jsonify, render_template
from logging import getLogger
from quart.logging import default_handler
import re

app = Quart(__name__)

points = json.load(open('points.json'))
cached_response = None
game = None
event_dicts = []
initialize_request_re = re.compile(r"^team(?P<id>\d+)_(?P<field>.+)")
initialize_lock = asyncio.Lock()
initialize_cond = asyncio.Condition()
cache_lock = asyncio.Lock()
game_state_queue = asyncio.Queue()
game_task = None
start_time = datetime.datetime.now().timestamp()

class Faction(enum.IntEnum):
    VS = 1
    NC = 2
    TR = 3
    NS = 4


class Team:
    id_iter = itertools.count(start=0)

    def __init__(self, name: str, members: dict[str:int], faction: Faction):
        self.members = members
        self.score = 0
        self.name = name
        self.faction = faction
        self.id = next(Team.id_iter)

    def __eq__(self, other):
        if isinstance(other, Team):
            return self.id == other.id

    def overlayAttributes(self):
        return {"name": self.name, "score": self.score, "faction": self.faction}

    def scorePoints(self, score: int):
        self.score += score

    def getPlayers(self) -> dict[str:int]:
        return {member: self.id for member in self.members.values()}


class Game:
    def __init__(self, teams: list[(str, dict[str:int], Faction)]):
        self.teams = [Team(*team) for team in teams]
        self.players = {}
        for team in self.teams:
            self.players.update(team.getPlayers())

    def __repr__(self):
        return json.dumps({"teams": [team.overlayAttributes() for team in self.teams]})

    def __str__(self):
        return "\n".join([f"{team.name}: {team.score}" for team in self.teams])

    def findPlayer(self, id: int) -> Optional[Team]:
        if (player := self.players.get(id, None)) is None:
            return None
        return self.teams[player]

    def getPlayer(self) -> list[int]:
        return self.players.values()

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
    game_task = asyncio.current_task()
    client = auraxium.event.EventClient(service_id='s:MEGABIGSTATS')
    async with initialize_lock:
        async with initialize_cond:
            while (not game):
                await initialize_cond.wait()
        print("Ready")

        @client.trigger(event.VehicleDestroy, characters=game.players.values())
        async def track_kill(evt: event.VehicleDestroy):

            victim_id = evt.character_id
            killer_id = evt.attacker_character_id
            killer_team = game.findPlayer(killer_id)
            victim_team = game.findPlayer(victim_id)
            killer_vehicle = evt.attacker_vehicle_id
            victim_vehicle = evt.vehicle_id

            # Ignore deaths not caused by enemy players
            if not victim_team or not killer_team or killer_id == 0 or victim_id == killer_id:
                pass
            elif killer_team == victim_team:
                killer_team.scorePoints(-(points[str(victim_vehicle)]["points"]))
            else:
                killer_team.scorePoints(points[str(victim_vehicle)]["points"])
            event_dict = {"Timestamp": evt.timestamp.isoformat(),
                          "Killer": killer_id, "Victim": victim_id,
                          "Killer Vehicle": points.get(str(killer_vehicle), {"name": "None"})["name"],
                          "Victim Vehicle": points.get(str(victim_vehicle), {"name": "None"})["name"]}
            event_dict.update(json.loads(game))
            event_dicts.append(event_dict)


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
async def initializeGame():
    if request.method == "GET":
        return await render_template("admin.html")
    global game
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


if __name__ == '__main__':
    try:
        app.run(host="localhost", port=8080)
        getLogger(app.name).removeHandler(default_handler)
    except KeyboardInterrupt:
        with open(f"log_{start_time}.csv", 'w', newline='') as csvfile:
            fieldnames = event_dicts[0].keys()
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for row in event_dicts:
                writer.writerow(row)
