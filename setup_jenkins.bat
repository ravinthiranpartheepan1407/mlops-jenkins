@echo off
echo Setting up ML Jenkins Project...

REM Create directory structure
mkdir src
mkdir tests
mkdir scripts
mkdir models
mkdir packages

echo Directory structure created!

REM Install Python dependencies
echo Installing Python dependencies...
pip install -r requirements.txt

echo Setup complete!
echo.
echo Next steps:
echo 1. Install Jenkins on Windows
echo 2. Create new Pipeline job in Jenkins
echo 3. Point Jenkins to your repository or upload Jenkinsfile
echo 4. Configure email notifications (optional)
echo 5. Run the pipeline!
