"""Authoritative Phase 5 Sonder House administrative configuration."""


def task(label, allow_na=False):
    return {"label": label, "allow_na": allow_na}


N = lambda label: task(label, True)
T = lambda label: task(label, False)

CATEGORIES = [
    ("front-desk", "Front Desk", 10),
    ("support", "Support", 20),
    ("awake-night", "Awake Night", 30),
    ("life-skills", "Life Skills", 40),
]

SHIFTS = [
    ("morning", "Morning", "07:00", "15:00", 10),
    ("evening", "Evening", "15:00", "23:00", 20),
    ("night", "Night", "23:00", "07:00", 30),
]

STAFF_ROSTER = [
    "Alfred Sampare", "Andrew Bevan", "Chelsea Brown", "Chrystal Auckland",
    "Daniel Buck", "Destiny Patelas", "Emmalee Pimentel", "Gary Hill",
    "Gurbinder Singh", "Jacob Larose", "Jaime Houlden", "Jaskirat Singh",
    "Jeremiah Gerow", "Jolene Williams", "Josh Hansen", "Kianna Coburn",
    "Konark Rawat", "Lucy Cabral", "Melodie Daigle", "Nadine Morgan",
    "Nayandeep Singh", "Nicole Morven", "Pranshukh Nayyar", "Ri Sharma",
    "Ron Bevan", "Roxanne Wagner", "Ruth McMillian", "Sandra Glover",
    "Sonya Kushnerek", "Vita Hunter",
]

FRONT_OFFICE_COMMON = [
    N("Accept and sort mail"),
    T("Review IR binder, Communication Book, WISH, Inclusion, schedule, and DNA list"),
    T("Check and reply to emails"),
    T("Complete leave requests and maintenance requests as needed"),
    N("Complete Citation training as required"),
    T("Photocopy and maintain an ample supply of paperwork"),
    N("Print needed forms"), T("Ensure printer is stocked with paper"),
    T("Monitor Facebook"), T("Review Ban Book and move completed bans to Completed"),
    T("Answer phone / intercom and document calls"),
]
FRONT_CALLS_COMMON = [
    N("Receive staff sick call-ins, complete the Staff Call-In Form, and update the schedule"),
    N("Complete staff call-outs by seniority; document attempts / outcomes"),
    N("Email the completed Staff Call-In Form once the call-out process is complete"),
]
FRONT_MONEY_COMMON = [
    T("Complete a money count at the beginning of the shift"),
    T("Manage canteen: stock items, process purchases, collect payment, and deposit cash"),
    N("Roll coins as needed"), N("Give any excess money to the supervisor"),
    T("Complete a money count at the end of the shift"),
]
FRONT_HOUSEKEEPING_DAY = [
    T("Sweep / mop / vacuum front entrance and lobby"), T("Sweep / mop office and Med Room"),
    T("Disinfect phone, door handles, pens, staplers, and other shared office items"),
    T("Dust / tidy office and remove clutter"), T("Empty garbage in office and Med Room"),
    T("Clean windows in entrance, Med Room, and office"),
    N("Shovel / salt front steps and walkway as needed"),
]
FRONT_HOUSEKEEPING_NIGHT = FRONT_HOUSEKEEPING_DAY[:5] + [N("Shovel / salt front steps and walkway as needed")]

FRONT_DESK_MORNING = [
    ("Office", FRONT_OFFICE_COMMON), ("Staff Call-In / Call-Out", FRONT_CALLS_COMMON),
    ("Money Handling / Canteen", FRONT_MONEY_COMMON), ("Housekeeping", FRONT_HOUSEKEEPING_DAY),
    ("Tenant / Site", [
        T("Greet / buzz in tenants"), T("Sign in visitors and agencies"),
        T("Ensure tenants receive messages and mail"), N("Accept pharmacy medication deliveries"),
        T("Document tenant interactions in WISH"), T("Monitor cameras"),
        T("Complete 12:00 PM wellness check calls"),
    ]),
]
FRONT_DESK_EVENING = [
    ("Office", FRONT_OFFICE_COMMON), ("Staff Call-In / Call-Out", FRONT_CALLS_COMMON),
    ("Money Handling / Canteen", FRONT_MONEY_COMMON), ("Housekeeping", FRONT_HOUSEKEEPING_DAY),
    ("Tenant / Site", [
        T("Greet / buzz in tenants"), T("Sign out visitors by 06:00 PM"),
        T("Ensure tenants receive messages and mail"), N("Accept pharmacy medication deliveries"),
        T("Document tenant interactions in WISH"), T("Monitor cameras"),
        T("Complete 08:00 PM wellness check calls"),
    ]),
]
FRONT_DESK_NIGHT = [
    ("Office", FRONT_OFFICE_COMMON[:10] + [T("Stock canteen"), N("Roll coins as needed"), T("Answer phone / intercom and document calls")]),
    ("Staff Call-In / Call-Out", [
        N("Receive staff sick call-ins, complete the Staff Call-In Form, and update the schedule"),
        N("Do not complete staff call-outs between 11:00 PM and 06:00 AM"),
        N("Complete staff call-outs by seniority; document attempts / outcomes"),
        N("Email the completed Staff Call-In Form once the call-out process is complete"),
    ]),
    ("Housekeeping", FRONT_HOUSEKEEPING_NIGHT),
    ("Tenant / Site", [T("Document tenant interactions in WISH"), T("Monitor cameras")]),
]

SUPPORT_OFFICE = [
    T("Carry cell phone, fanny pack, and walkie-talkie"),
    T("Review IR binder, Communication Book, WISH, Inclusion, schedule, and DNA list"),
    N("Review Heat Hut"), T("Check and reply to emails"),
    T("Complete leave requests and maintenance requests as needed"),
    N("Complete Citation training as required"),
    T("Organize / tidy Support cupboards and keep work area clean, organized, and dusted"),
    T("Complete shift exchange"), N("Shop for canteen / tenant supplies"),
]
SUPPORT_TENANT = [
    T("Document tenant interactions in WISH"), N("Complete Incident Reports as needed"),
    T("Spend mealtime in the Common Room"), T("Complete programming and fill out programming report"),
    N("Complete 1-on-1 meetings with tenants"), N("Assist tenants with Heat Hut and label items"),
]
LAUNDRY = [T("Clean / tidy first floor laundry room"), T("Clean / tidy second floor laundry room"), T("Clean / tidy third floor laundry room")]
HALLWAYS = [T("Sweep / mop first floor hallway"), T("Sweep / mop second floor hallway"), T("Sweep / mop third floor hallway")]
BATHROOMS = [T("Clean staff bathroom"), T("Clean hallway staff bathroom"), T("Clean accessible tenant washroom")]

SUPPORT_MORNING = [
    ("Office", SUPPORT_OFFICE), ("Tenant Support", SUPPORT_TENANT),
    ("Common Room", [
        T("Wipe down Common Room tables and TV chairs; stack Common Room chairs"),
        T("Sweep / mop Common Room"), T("Vacuum Common Room carpet"), T("Take out Common Room garbage"),
        T("Ensure coffee area is tidy and stocked"), N("Water plants as needed"),
    ]),
    ("Laundry Rooms", LAUNDRY), ("Hallways", HALLWAYS), ("Bathrooms", BATHROOMS),
    ("Safety / Building Checks", [
        T("Complete perimeter and exit safety check, including gates and debris pickup"),
        T("Ensure all fire doors are closed"), T("Complete hourly checks on all floors for parties / messes"),
    ]),
]
SUPPORT_EVENING = [
    ("Office", SUPPORT_OFFICE), ("Tenant Support", SUPPORT_TENANT),
    ("Common Room", [
        T("Wipe down Common Room tables and TV chairs; stack Common Room chairs"),
        T("Sweep / mop Common Room"), T("Vacuum Common Room carpet"), T("Take out Common Room garbage"),
        T("Ensure coffee area is tidy"), N("Water plants as needed"),
    ]),
    ("Laundry Rooms", LAUNDRY), ("Hallways", HALLWAYS), ("Bathrooms", BATHROOMS),
    ("Janitor Rooms", [T("Clean / tidy first floor janitor room"), T("Clean / tidy second floor janitor room"), T("Clean / tidy third floor janitor room")]),
    ("Safety / Building Checks", [
        T("Complete perimeter and exit safety check, including gates and debris pickup"),
        T("Ensure all fire doors are closed"), T("Complete hourly checks on all floors for parties / messes"),
        T("Remove clutter from 1-on-1 room and staff room"),
    ]),
]

AWAKE_NIGHT = [
    ("Office", [
        T("Carry cell phone, fanny pack, and walkie-talkie"),
        T("Review IR binder, Communication Book, WISH, Inclusion, schedule, and DNA list"),
        T("Check and reply to emails"), N("Submit leave requests as needed"),
        N("Complete Citation training as required"), T("Keep work area clean, organized, and dusted"),
        T("Complete shift exchange"), T("Document tenant interactions in WISH"), N("Complete Incident Reports as needed"),
    ]),
    ("Tenant Support / Building Checks", [T("Complete hourly floor walks, including corridors, perimeter, and door checks"), T("Offer tenants support")]),
    ("Laundry Rooms", LAUNDRY + [T("Wash, dry, and put away rags, mop heads, and dusters")]),
    ("Maintenance Rooms", [T("Clean / tidy both first floor maintenance rooms"), T("Clean / tidy both second floor maintenance rooms"), T("Clean / tidy both third floor maintenance rooms")]),
    ("Janitor Rooms", [T("Clean, organize, and stock first floor janitor room"), T("Clean, organize, and stock second floor janitor room"), T("Clean, organize, and stock third floor janitor room")]),
    ("Hallways", HALLWAYS), ("Stairwells", [T("Sweep / mop east stairwell"), T("Sweep / mop west stairwell")]),
    ("Bathrooms", BATHROOMS),
    ("Common Room", [T("Clean blinds and ledges in Common Room"), T("Deep clean TV chairs in Common Room"), T("Deep clean Common Room floors and ledges")]),
    ("Other Cleaning / Closing Tasks", [T("Empty all garbage cans inside and outside"), T("Sweep / mop staff room and 1-on-1 room; disinfect surfaces"), T("Make coffee at 05:00 AM")]),
]

LIFE_SKILLS = [
    ("Office / Documentation", [
        T("Complete daily planning"), T("Document tenant interactions in WISH"), T("Check and reply to emails"),
        N("Complete Citation training as required"), T("Complete required documentation"), N("Update forms as needed"),
        N("Complete tenant case planning"), T("Organize / maintain tenant binders"),
        T("File current tenant bans in appropriate binders"), T("Keep office space tidy and put personal belongings away in locker"),
        T("Save all documents to the computer and log off at the end of the shift"),
    ]),
    ("Tenant Support", [
        T("Provide morning rides"), T("Provide afternoon rides"), N("Provide ride to Food Share"),
        T("Provide in-unit cleaning and Life Skills support"), T("Prepare units for move-ins"),
        T("Review items needed for tenant units and email the manager for requested items"),
        T("Connect with the Ministry as needed to support tenants"),
    ]),
    ("Inspection / Unit Check", [
        N("Prepare for weekly inspections"), N("Complete weekly inspections"), N("Complete inspection reports"),
        N("Complete outstanding inspection documentation / report follow-up"), N("Complete monthly bed bug checks"),
    ]),
    ("Life Skills / Independence Support", [
        N("Assist tenants with identification: Services Card, Birth Certificate, and Status Card"),
        N("Assist tenants with financials: banking, accounting, budgeting, income, and benefits"),
        N("Provide Life Skills support: cooking, hygiene, household skills, literacy"),
        N("Provide Life Skills support: computer skills, job search, and goal management"),
        N("Review tenant Life Skills needs / goals and follow up on identified supports"),
    ]),
]

ASSIGNMENTS = [
    ("front-desk", "morning", "Front Desk Morning", 10, FRONT_DESK_MORNING),
    ("front-desk", "evening", "Front Desk Evening", 20, FRONT_DESK_EVENING),
    ("front-desk", "night", "Front Desk Night", 30, FRONT_DESK_NIGHT),
    ("support", "morning", "Support Morning", 40, SUPPORT_MORNING),
    ("support", "evening", "Support Evening", 50, SUPPORT_EVENING),
    ("awake-night", "night", "Awake Night", 60, AWAKE_NIGHT),
    ("life-skills", "morning", "Life Skills Morning", 70, LIFE_SKILLS),
]

PROGRAMMING_TASK_LABEL = "Complete programming and fill out programming report"
