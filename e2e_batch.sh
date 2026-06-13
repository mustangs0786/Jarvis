#!/bin/bash
# Sequential E2E batch — remaining URLs, headless, isolated profile, one log each.
cd "$(dirname "$0")"
export APPLY_PROFILE_DIR=browser_profile_e2e
export APPLY_HEADLESS=1

run() {
  echo "=== $(date +%H:%M:%S) START $1 ==="
  uv run python e2e_test_run.py "$2" > "$3" 2>&1
  echo "=== $(date +%H:%M:%S) END $1 (exit $?) ==="
}

run "URL3-Jumio"     'https://job-boards.greenhouse.io/jumio/jobs/4627200005'                                            /tmp/e2e_url3_jumio.log
run "URL4-Sandisk"   'https://jobs.smartrecruiters.com/Sandisk/744000131427660-senior-engineer-machine-learning-'        /tmp/e2e_url4_sandisk.log
run "URL2-Kyndryl"   'https://kyndryl.wd5.myworkdayjobs.com/KyndrylProfessionalCareers/job/Bangalore-Karnataka-India/AI-ML-Specialist_R-61116' /tmp/e2e_url2_kyndryl.log
run "URL5-HPE"       'https://careers.hpe.com/us/en/job/1206758/AI-and-Machine-Learning-Engineer'                        /tmp/e2e_url5_hpe.log
run "URL1-Glean"     'https://job-boards.greenhouse.io/gleanwork/jobs/4012745005'                                        /tmp/e2e_url1_final.log
echo "BATCH COMPLETE"
