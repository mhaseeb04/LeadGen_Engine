"""
us_cities.py — static US state -> major cities dataset.

Source: Craigslist regional site index (geo.craigslist.org/iso/us),
parsed and merged with a hand-verified major-metro list, with each
state's capital guaranteed as a fallback. Baked in as a static file
(not fetched live) for speed and reliability — no runtime dependency
on a third-party site staying up or unblocked.

Powers:
  • GET /api/cities?state=<state> — populates the city dropdown/datalist
  • query_parser.py city recognition (merged in at import time)
"""

CITIES_BY_STATE: dict[str, list[str]] = {
    'Alabama': ['Auburn', 'Birmingham', 'Dothan', 'Mobile', 'Montgomery', 'Tuscaloosa'],
    'Alaska': ['Anchorage', 'Fairbanks', 'Juneau'],
    'Arizona': ['Mesa', 'Phoenix', 'Prescott', 'Scottsdale', 'Show Low', 'Sierra Vista', 'Tucson', 'Yuma'],
    'Arkansas': ['Fayetteville', 'Fort Smith', 'Jonesboro', 'Little Rock'],
    'California': ['Bakersfield', 'Chico', 'Fresno', 'Inland Empire', 'Long Beach', 'Los Angeles', 'Merced', 'Modesto', 'Oakland', 'Orange County', 'Palm Springs', 'Redding', 'Sacramento', 'San Diego', 'San Francisco', 'San Jose', 'Santa Maria', 'Stockton', 'Susanville', 'Yuba-Sutter'],
    'Colorado': ['Boulder', 'Colorado Springs', 'Denver', 'Pueblo'],
    'Connecticut': ['Hartford', 'New Haven'],
    'Delaware': ['Dover', 'Wilmington'],
    'Florida': ['Fort Lauderdale', 'Gainesville', 'Jacksonville', 'Lakeland', 'Miami', 'Ocala', 'Orlando', 'Panama City', 'Pensacola', 'Sarasota', 'Space Coast', 'St Augustine', 'St Petersburg', 'St. Petersburg', 'Tallahassee', 'Tampa', 'Treasure Coast'],
    'Georgia': ['Albany', 'Athens', 'Atlanta', 'Augusta', 'Brunswick', 'Columbus', 'Savannah', 'Statesboro', 'Valdosta'],
    'Hawaii': ['Honolulu'],
    'Idaho': ['Boise', 'Twin Falls'],
    'Illinois': ['Chicago', 'Decatur', 'Peoria', 'Rockford', 'Springfield'],
    'Indiana': ['Bloomington', 'Evansville', 'Fort Wayne', 'Indianapolis', 'Kokomo', 'Richmond', 'Terre Haute'],
    'Iowa': ['Ames', 'Cedar Rapids', 'Des Moines', 'Fort Dodge', 'Iowa City', 'Mason City', 'Sioux City'],
    'Kansas': ['Lawrence', 'Manhattan', 'Salina', 'Topeka', 'Wichita'],
    'Kentucky': ['Bowling Green', 'Frankfort', 'Lexington', 'Louisville', 'Owensboro'],
    'Louisiana': ['Baton Rouge', 'Houma', 'Lafayette', 'Lake Charles', 'Monroe', 'New Orleans', 'Shreveport'],
    'Maine': ['Augusta'],
    'Maryland': ['Annapolis', 'Baltimore', 'Frederick'],
    'Massachusetts': ['Boston', 'South Coast', 'Worcester'],
    'Michigan': ['Ann Arbor', 'Battle Creek', 'Detroit', 'Flint', 'Grand Rapids', 'Holland', 'Jackson', 'Kalamazoo', 'Lansing', 'Monroe', 'Muskegon', 'Port Huron', 'The Thumb', 'Upper Peninsula'],
    'Minnesota': ['Bemidji', 'Brainerd', 'Mankato', 'Minneapolis', 'Rochester', 'St Cloud', 'St Paul', 'St. Paul'],
    'Mississippi': ['Hattiesburg', 'Jackson', 'Meridian'],
    'Missouri': ['Jefferson City', 'Joplin', 'Kansas City', 'Kirksville', 'Springfield', 'St Louis', 'St. Louis'],
    'Montana': ['Billings', 'Bozeman', 'Butte', 'Great Falls', 'Helena', 'Kalispell', 'Missoula'],
    'Nebraska': ['Grand Island', 'Lincoln', 'North Platte', 'Omaha'],
    'Nevada': ['Carson City', 'Elko', 'Henderson', 'Las Vegas', 'Reno'],
    'New Hampshire': ['Concord', 'Manchester'],
    'New Jersey': ['Jersey City', 'Newark', 'Trenton'],
    'New Mexico': ['Albuquerque', 'Farmington', 'Las Cruces', 'Santa Fe'],
    'New York': ['Albany', 'Binghamton', 'Buffalo', 'Chautauqua', 'Finger Lakes', 'Glens Falls', 'Hudson Valley', 'Ithaca', 'Long Island', 'New York', 'New York City', 'Nyc', 'Oneonta', 'Rochester', 'Syracuse', 'Watertown'],
    'North Carolina': ['Asheville', 'Boone', 'Charlotte', 'Durham', 'Fayetteville', 'Greensboro', 'Jacksonville', 'Raleigh', 'Wilmington', 'Winston-Salem'],
    'North Dakota': ['Bismarck', 'Fargo'],
    'Ohio': ['Ashtabula', 'Athens', 'Chillicothe', 'Cincinnati', 'Cleveland', 'Columbus', 'Mansfield', 'Sandusky', 'Toledo', 'Youngstown'],
    'Oklahoma': ['Lawton', 'Oklahoma City', 'Stillwater', 'Tulsa'],
    'Oregon': ['Bend', 'Eugene', 'Klamath Falls', 'Portland', 'Roseburg', 'Salem'],
    'Pennsylvania': ['Erie', 'Harrisburg', 'Lancaster', 'Meadville', 'Philadelphia', 'Pittsburgh', 'Reading', 'State College', 'Williamsport', 'York'],
    'Rhode Island': ['Providence'],
    'South Carolina': ['Charleston', 'Columbia', 'Florence', 'Myrtle Beach'],
    'South Dakota': ['Pierre', 'Sioux Falls'],
    'Tennessee': ['Chattanooga', 'Clarksville', 'Cookeville', 'Jackson', 'Knoxville', 'Memphis', 'Nashville', 'Tri-Cities'],
    'Texas': ['Abilene', 'Amarillo', 'Austin', 'Brownsville', 'College Station', 'Corpus Christi', 'Dallas', 'El Paso', 'Fort Worth', 'Galveston', 'Houston', 'Laredo', 'Lubbock', 'Plano', 'San Angelo', 'San Antonio', 'San Marcos', 'Victoria', 'Waco', 'Wichita Falls'],
    'Utah': ['Logan', 'Provo', 'Salt Lake City', 'St George'],
    'Vermont': ['Burlington', 'Montpelier'],
    'Virginia': ['Charlottesville', 'Fredericksburg', 'Harrisonburg', 'Lynchburg', 'Norfolk', 'Richmond', 'Roanoke', 'Virginia Beach', 'Washington', 'Winchester'],
    'Washington': ['Bellingham', 'Moses Lake', 'Olympia', 'Seattle', 'Spokane', 'Wenatchee', 'Yakima'],
    'West Virginia': ['Charleston', 'Morgantown'],
    'Wisconsin': ['Eau Claire', 'Green Bay', 'Janesville', 'La Crosse', 'Madison', 'Milwaukee', 'Sheboygan', 'Wausau'],
    'Wyoming': ['Cheyenne'],
}

