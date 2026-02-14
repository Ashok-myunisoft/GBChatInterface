# TODO: Fix Leave Type Options Not Showing

## Completed Steps
- [x] Update payload in get_leave_types to remove restrictive LeaveType filter
- [x] Add debug prints to log payload, API response, and parsed leave types
- [x] Change API endpoint from Leave.svc to LeaveType.svc
- [x] Change API endpoint from LeaveType.svc to TLeaveType.svc

## Next Steps
- [ ] Run the application and test the leave apply flow to verify options appear
- [ ] Check debug logs for any issues with API response or parsing
- [ ] If options still don't show, investigate further (e.g., API endpoint, login credentials)
