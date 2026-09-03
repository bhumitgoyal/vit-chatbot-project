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
| faculty search | `hrms/employeeSearchForStudent` | `searchName` | not re-verified this pass |
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

## Confirmed but deliberately NOT wired in

- `studentBankInformation/BankInfoStudent` — shows the student's **bank account number** in plain text. Too sensitive for a chatbot.
- `proctor/viewStudentCredentials` — the student's own login credentials. Never surface.
- `finance/Payments` — attempted/failed online transactions; `receipts` already covers completed payments.
- `academics/common/CalendarPreview` — needs `semesterSubId` + `classGroupId`; low value.

## Best-effort / unconfirmed

- `assignments` → `examinations/StudentDA` — landing page; may need a follow-up POST.
- `hrms/employeeSearchForStudent` faculty search — parser not re-verified.
- `attendance_detail` — endpoint confirmed; the `registerNumber` token format
  (`VL_<CODE>_00100`) is derived, not read from the attendance table.
