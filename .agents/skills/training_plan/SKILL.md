---
name: training_plan
description: Planning personal marathon training schedule. Prompt is from https://www.runbung.app/en/training-prompt
---

## Required inputs

The user must provide at least one of the following:

- `Race Distance`: target race distance (e.g., full, half, 42.195km, 23.2 miles, 21.1km, 10km, ...)
- `Target Time`: target race time (e.g., sub-3 hours)
- `Preferred Units`: km or miles (default is km unless specified)
- `Experience Level`: Beginner (No marathon experience) / Intermediate (completed at least one race) / Advanced (multiple race completion experience)
- `Current Weekly Mileage`
- `Longest Recent Run`
- `Total Training Duration`: planned training duration (e.g., 12 weeks)
- `Selected Training Days`: Mon-Sun
- `Preferred Quality Day`
- `Preferred Long run Day`
- `Preferred Language`: Dafault: Korean

## If required inputs are missing

- Before proceeding, ask the user the following default information looks okay to them:
- If `Race Distance` is missing, use 42.195km as the default distance.
- If `Target Time` is missing, infer the best goal of the user based on the existing training/race records. If there is no record, there is no target time, and goal is to finish the race ("Finish Only")
- If `Current Weekly Mileage` and `Longest Recent Run` are not given, use the recent 3 months records from `Records/Strava`.
- If `Total Training Duration` or `Selected Training Days` is missing, ask the user to provide them before proceeding.
- If `Preferred Quality Day` and `Preferred Long run Day` are missing, ask the user to provide them before proceeding. If user says that there is no preference, infer the best days for training.

## Workflow

Please generate a systematic and consistent week-by-week training plan in a table format based on the following runner information:

**1. Race & Goal:**
* Race Distance: `Race Distance`
* Goal: `Target Time`
* Preferred Units: kilometers

**2. Runner Profile:**
* Experience Level: `Experience Level`
* Current Weekly Mileage: `Current Weekly Mileage`
* Longest Recent Run (within the last 1-2 months): `Longest Recent Run`
* You can use more information stored into `Info/`, such as current VO2Max, Running Lactate Threshold, max and rest heart rates, ... if exists any.

**3. Training Availability:**
* Total Training Duration: `Total Training Duration` weeks
* Selected Training Days: `Selected Training Days` - Schedule workouts *only* on these specific days.
* Preferred Quality Day: `Preferred Qaulity Day`
* Preferred Long run Day: `Preferred Long run Day`

**Requirements for the Plan:**
1.  Present the output as a **week-by-week table**. Columns must include: **Week #, Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday** (showing workout type and distance/duration in kilometers for scheduled days, or "Rest" for non-selected days), and **Total Weekly Mileage** (in kilometers).
2.  Ensure **gradual and sensible progression** of weekly mileage and the weekly long run distance.
3.  Intelligently distribute workout types across the `Selected Training Days`. Schedule rest days on all non-selected days.
4.  Include essential workouts: **Long Runs** (schedule strategically, considering weekend days if selected among training days), **Easy Runs**, and appropriate **Quality Sessions** (e.g., Tempo runs, Interval training) suitable for the specified Goal and Experience Level.
5.  Implement a standard **Taper Period** (reduced training volume) for the final 2-3 weeks before the race.
6.  If a specific Target Time is provided in Target Time: `Target Time`, **calculate and provide specific target training paces** (e.g., for Easy Runs, Long Runs, Tempo Runs, Interval Runs). **These paces are a required part of the output.** Ideally, include relevant paces directly within the weekly schedule descriptions (e.g., 'Tempo Run: 6 kilometers @ X:XX/kilometers pace') or list them clearly in a separate summary table accompanying the main schedule.
7.  The generated plan must be **systematic, internally consistent, and follow generally accepted training principles** suitable for preparing for a 42.195 kilometers race.

## Output format

Please provide the response in `Preferred Langauge`.
After explaining the training plan, provide a markdown file under `Plans/` directory with name of `YYYY-MM-DD-X-Week-Plans`, where `YYYY-MM-DD` is the current date, unless the user asks otherwise.
Make sure that the markdown grammer is correct (especially for the table)
