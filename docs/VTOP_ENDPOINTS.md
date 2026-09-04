# VTOP endpoints used by VITopia AI

All paths are relative to `https://vtop.vit.ac.in/vtop/`. Every authenticated
request is `POST` with body `authorizedID=<REG>&_csrf=<csrf>&x=<UTC timestamp>`
(+ `semesterSubId` where noted). `_csrf` comes from `/content` after login.

Confirmed against a live 2026 student login on 2026-09-03 (menu `data-url`
attributes + response inspection). `GET /api/debug/vtop[?module=…]` on a running
instance with a connected session re-dumps these if VTOP changes.

## Auth

| Step | Path | Notes |
| --- | --- | --- |
| Bootstrap | `GET initialProcess` | `#stdForm` → `_csrf`, `flag` |
| Prelogin | `POST prelogin/setup` (`_csrf,flag=VTOP`) | `#captchaBlock img` data-URI — **only on some attempts** → retry loop |
| Login | `POST login` (`_csrf,username,password,captchaStr`) | fail → `login/error` / "Invalid Captcha" / "Invalid User ID" |
| Dashboard | `GET content` | real `_csrf`, `var id`, and a **"CGPA and CREDIT Status"** block (`Current CGPA`, `Earned Credits`, `Total Credits Required`) — parsed at login for the authoritative CGPA |
| Logout | `GET logout` | — |

## Data modules — all confirmed working

| Module | Path | Body | Shape |
| --- | --- | --- | --- |
| `attendance` | `processViewStudentAttendance` | `semesterSubId` | `Sl │ Class Group │ Course │ Class Detail │ Faculty │ Attended │ Total │ % │ Debar │ link` |
| `timetable` / `courses` | `processViewTimeTable` | `semesterSubId` | `Sl │ Class Group │ Course │ L T P J C │ Category │ Option │ ClassId │ Slot/Venue │ Faculty │ RegDate │ AttDate │ Status` |
| `marks` | `examinations/StudentMarkView` (GET) → `examinations/doStudentMarkView` | `semesterSubId` | nested tables: course header `Sl │ VL… │ Code │ Title │ …`; mark row `Sl │ Title │ Max │ Weightage% │ Status │ Scored │ WeightMark │ Remark` (dedup) |
| `grades` | `examinations/examGradeView/StudentGradeHistory` | — | identity row + grade rows `Sl │ Code │ Title │ Type │ Credits │ Grade │ ExamMonth │ ResultDate │ Dist │ Detail`. CGPA taken from the dashboard; grade counts / transcript from here. |
| `exam_schedule` | `examinations/doSearchExamScheduleForStudent` | `semesterSubId` | 1-cell section rows `FAT`/`CAT1`/`CAT2`; 13-cell data rows `Sl │ Code │ Title │ Type │ ClassId │ Slot │ Date │ Session │ Reporting │ ExamTime │ Venue │ SeatLoc │ SeatNo` |
| `curriculum` | `academics/common/Curriculum` | — | plain text: `Total Credits: N` then repeating `<CODE> Credit: <earned> Max. Credit: <max> <Basket Name>` |
| `profile` (also serves hostel & mess) | `studentsRecord/StudentProfileAllView` | — | key/value rows. Used: `STUDENT NAME`, `DATE OF BIRTH`, `GENDER`, `BLOOD GROUP`, `HOSTELLER`, `Block Name`, `Room No.`, `Bed Type`, `Mess Information` |
| `proctor` | `proctor/viewProctorDetails` | — | KV: `Faculty Name`, `Faculty Designation`, `School`, `Cabin`, `Faculty Department`, `Faculty Email`, `Faculty intercom`, `Faculty Mobile Number` |
| `proctor_messages` | `proctor/viewMessagesSendByProctor` | — | usually empty → "no messages" |
| `hod_dean` | `hrms/viewHodDeanDetails` | — | KV: dean/HoD name, email, cabin (parser is best-effort; falls back to `bot/school_directory.py`) |
| `receipts` | `finance/getStudentReceipts` | — | `Invoice Number │ Receipt Number │ Date │ Amount │ Campus Code │ View` |
| faculty search (list) | `hrms/EmployeeSearchForStudent` (capital E) | `searchEmployee` (≥3 chars) | `Name of the Faculty │ Designation │ School / Centre │ Action` — the Action `<button id="…">` carries the numeric `empId` |
| faculty search (detail) | `hrms/EmployeeSearch1ForStudent` | `empId` (from the list step) | KV: `Name of the Faculty, Designation, Name of Department, School / Centre Name, E-Mail Id, Cabin Number` + an "Open Hours" day/time table |
| semester list | `academics/common/StudentTimeTable` | — | `<select id="semesterSubId">`; current = `VL20262701` |

## Added 2026-09-03 (second menu crawl)

| Module | Path | Params | Shape |
| --- | --- | --- | --- |
| `attendance_detail` | `processViewAttendanceDetail` | `semesterSubId`, `authorizedID`, `registerNumber`=`VL_<CODE>_00100`, `courseType`=`TH`/`LO` | per-course day log: `Sl │ Date │ Slot │ Day/Time │ Status` + summary (present/absent/on-duty/%). Triggered when the student names a course in an attendance question. |
| `biometric` | `getStudViewBioList` | `fromDate` (`DD-MMM-YYYY`), `authorizedID`, `_csrf` | campus biometric punch log for a day; "No Record(S) Found" when none. |
| `class_messages` | `academics/common/StudentClassMessage` | `semesterSubId` | faculty→class messages; "No Messages Sent by Faculty" when empty. |
| `library_dues` | `finance/libraryPayments` | — | `Koha Due Amount ₹ 0.00`. |
| `fee_intimations` | `finance/getStudentFeesIntimation` | — | `Sl │ Fee Year │ Fee Term │ Fees Description │ Letter`. |
| `additional_learning` | `academics/additionalLearning/AdditionalLearningStudentView` | — | Minor / Honour registrations. |
| `scholarships` | `admissions/getStudentScholarshipDetails` | — | `Sl │ Scholarship Name │ Source │ Address │ Received Date │ File`. |

## Added 2026-09-04 (third menu crawl)

| Module | Path | Params | Shape |
| --- | --- | --- | --- |
| `project_work` | `academics/common/ProjectView` | `semesterSubId` (select + Submit) | `Course Code │ Course Title │ Status │ View`, e.g. `BCSE497J │ Project - I │ Registered and Approved by Guide`. The page's own "View/Edit" confirm dialog echoed `Course Id: VL_BCSE497J_00100` — this **confirms** the `VL_<CODE>_00100` token format used by `attendance_detail`. |

Course/project **View/Edit** links open an edit-confirmation dialog — never
click "Proceed" there; the chatbot only reads the registration-status table,
never the edit flow.

## Confirmed but deliberately NOT wired in

- `studentBankInformation/BankInfoStudent` — shows the student's **bank account number** in plain text. Too sensitive for a chatbot.
- `proctor/viewStudentCredentials` — the student's own login credentials. Never surface.
- `finance/Payments` — attempted/failed online transactions; `receipts` already covers completed payments.
- `academics/common/CalendarPreview` — renders a month-tab calendar widget (not a table); needs `semesterSubId` + `classGroupId`. Institutional, not personal — low value for the parsing effort.
- `admissions/SpecialAchieversAwards` — this is a **submission form** for claiming an achievement (event category/theme/type selects), not a read view of existing ones.
- `examinations/malpracticePunishmentDetails` — the general Code-of-Conduct penalty table (institutional policy), not the student's own disciplinary record. Candidate for the RAG knowledge base instead of a live module.
- `admissions/costCentreCircularsViewPageController` — static list of old circular PDFs (2019–2021), not personal, not current.
- `academics/common/ExtraCurricular`, `examinations/arrearRegistration/LoadRegularArrearViewPage` — endpoints exist but returned empty for the test account (no EC records / no backlogs); response shape unconfirmed. Re-probe with an account that has data before wiring in.
- `academics/common/QCMStudentLogin` — course-feedback survey form, not informational.

## Best-effort / unconfirmed

- `assignments` → `examinations/StudentDA` — landing page; may need a follow-up POST.
- `attendance_detail` — endpoint confirmed; the `registerNumber` token format
  (`VL_<CODE>_00100`) is now cross-confirmed via `project_work`'s confirm dialog.

## Fixed 2026-09-04 (faculty search was hitting the wrong path/params)

The original faculty-search scraper POSTed to the lowercase
`hrms/employeeSearchForStudent` (that's only the GET *landing page* path) with
`searchName`/`empName` params that VTOP never reads, so it silently always
fell back to the unverified static directory. A live search verified the real
two-step flow (`EmployeeSearchForStudent` list → `EmployeeSearch1ForStudent`
detail, see table above) and `search_faculty_live` now uses it — confirmed
real email `spmeenakshi@vit.ac.in` for MEENAKSHI S P (the static fallback had
guessed `meenakshi.sp@vit.ac.in`, wrong). The chat now also triggers a lookup
on a bare `"<Name> email"` / `"who is professor <Name>"`, not only when the
word "faculty" appears.
