pipeline {
    agent any

    environment {
        PYTHON_PATH = 'C:/Users/ravin/AppData/Local/Programs/Python/Python312/python.exe'
        VENV_PATH = '.venv'
    }

    stages {
        stage('Setup') {
            steps {
                echo 'Setting up Python environment...'
                bat '''
                    %PYTHON_PATH% -m venv %VENV_PATH%
                '''

                // Upgrade pip using the correct method
                bat '''
                    %VENV_PATH%\\Scripts\\python.exe -m pip install --upgrade pip
                '''

                // Install requirements
                bat '''
                    %VENV_PATH%\\Scripts\\activate.bat && pip install -r requirements.txt
                '''
            }
        }

        stage('Test') {
            steps {
                echo 'Running unit tests...'
                bat '''
                    %VENV_PATH%\\Scripts\\activate.bat && python -m pytest tests/ -v --tb=short
                '''
            }
            post {
                always {
                    echo 'Tests completed'
                    // Optionally publish test results
                    // publishTestResults testResultsPattern: 'test-results.xml'
                }
            }
        }

        stage('Train Model') {
            steps {
                echo 'Training ML model...'
                bat '''
                    %VENV_PATH%\\Scripts\\activate.bat && python scripts/train_model.py
                '''
            }
            post {
                success {
                    echo 'Model training completed successfully'
                }
                failure {
                    echo 'Model training failed'
                }
            }
        }

        stage('Package Model') {
            steps {
                echo 'Packaging trained model...'
                bat '''
                    %VENV_PATH%\\Scripts\\activate.bat && python scripts/package_model.py
                '''
            }
            post {
                success {
                    // Archive the packaged model
                    archiveArtifacts artifacts: 'packages/*.zip', allowEmptyArchive: false
                    echo 'Model packaged and archived successfully'
                }
            }
        }
    }

    post {
        success {
            echo 'Pipeline completed successfully!'
            emailext (
                subject: "SUCCESS: ML Pipeline Build #${BUILD_NUMBER}",
                body: """
                    The ML model pipeline completed successfully.

                    Build Details:
                    - Build Number: ${BUILD_NUMBER}
                    - Build URL: ${BUILD_URL}
                    - Workspace: ${WORKSPACE}

                    Model is ready for deployment.
                """,
                to: "surenpartheepan1407@gmail.com"
            )
        }
        failure {
            echo 'Pipeline failed!'
            emailext (
                subject: "FAILED: ML Pipeline Build #${BUILD_NUMBER}",
                body: """
                    The ML model pipeline failed.

                    Build Details:
                    - Build Number: ${BUILD_NUMBER}
                    - Build URL: ${BUILD_URL}
                    - Console Output: ${BUILD_URL}console

                    Please check the logs for more details.
                """,
                to: "surenpartheepan1407@gmail.com"
            )
        }
        cleanup {
            echo 'Cleaning up...'
            // Clean up virtual environment if needed
            bat '''
                if exist %VENV_PATH% rmdir /s /q %VENV_PATH%
            '''
        }
    }
}