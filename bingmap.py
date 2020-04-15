import requests
import numpy as np

import json
with open('api.json', 'r') as f:
    data = json.load(f)
bing_map_key = data['bingmap']
weather_key = data['hefengweather']

def get_coordinate(content):
    url = "https://dev.ditu.live.com/REST/v1/Locations?query=" + content + "&key=" + bing_map_key
    r = requests.get(url)
    result = r.json()
    address = result['resourceSets'][0]['resources'][0]['address']
    address = address['countryRegion'] + ' ' + address['formattedAddress']
    coordinate = result['resourceSets'][0]['resources'][0]['point']['coordinates']
    if coordinate[0] > 0:
        avec = 'N'
    else:
        avec = 'S'
    if coordinate[1] > 0:
        bvec = 'E'
    else:
        bvec = 'W'
    coordinate = str(np.round(coordinate[0],3)) + avec +' '+ str(np.round(coordinate[1],3)) + bvec
    string = address + '：\n' + coordinate
    return string

def get_route(content):
    start, end = '北京玉泉路地铁站','北京南站'
    time = '3:00:00PM'
    transiturl = 'https://dev.ditu.live.com/REST/V1/Routes/Transit?wp.0= '+ start + '&wp.1=' + end + '&timeType=Departure&dateTime='+ time +'&output=json&key='+bing_map_key
    r = requests.get(transiturl)

    distanceUnit = r.json()['resourceSets'][0]['resources'][0]['distanceUnit']
    distance = r.json()['resourceSets'][0]['resources'][0]['travelDistance']
    durationUnit = r.json()['resourceSets'][0]['resources'][0]['durationUnit']
    duration = r.json()['resourceSets'][0]['resources'][0]['travelDuration']

    route = r.json()['resourceSets'][0]['resources'][0]['routeLegs'][0]

    string = '出发 ' + start + ' ' + str(route['actualStart']['coordinates']) + '\n'
    string = string + '到达 ' + end + ' ' + str(route['actualEnd']['coordinates']) + '\n'
    print(string)

    for i in route['itineraryItems']:
        string += i['instruction']['text']+'\n'
    return string

def get_weather(content):
    url = "https://dev.ditu.live.com/REST/v1/Locations?query=" + content + "&key=" + bing_map_key
    r = requests.get(url)
    result = r.json()
    locations = result['resourceSets'][0]['resources'][0]['point']['coordinates']
    locations = ','.join([str(i) for i in locations])
    methods = ['now','forecast','hourly','lifestyle']
    try:
        url = 'https://free-api.heweather.net/s6/weather/'+methods[0]
        data = {'location':locations, 'key':weather_key}
        r = requests.post(url,data=data)
        tmp1 = r.json()
        url = 'https://free-api.heweather.net/s6/weather/'+methods[3]
        data = {'location':locations, 'key':weather_key}
        r = requests.post(url,data=data)
        tmp2 = r.json()
        weather = {'地点':content,'更新时间':tmp1['HeWeather6'][0]['update']['loc'],
          '天气':tmp1['HeWeather6'][0]['now']['cond_txt'],
          '气温/体感温度':tmp1['HeWeather6'][0]['now']['tmp']+'/'+tmp1['HeWeather6'][0]['now']['fl'],
          '风向/风力/风速':tmp1['HeWeather6'][0]['now']['wind_dir']+'/'+tmp1['HeWeather6'][0]['now']['wind_sc']+'/'+tmp1['HeWeather6'][0]['now']['wind_spd'],
          '云量':tmp1['HeWeather6'][0]['now']['cloud'],
          '湿度':tmp1['HeWeather6'][0]['now']['hum'],
          '降水量':tmp1['HeWeather6'][0]['now']['pcpn'],
          '能见度':tmp1['HeWeather6'][0]['now']['vis'],
          '舒适度':tmp2['HeWeather6'][0]['lifestyle'][0]['brf']+' '+tmp2['HeWeather6'][0]['lifestyle'][0]['txt']}
        string = ''
        for i in weather:
            string += i+'：'+weather[i]+'\n'
        return string[:-1]
    except:
        return '国外的天气我就不清楚了。'