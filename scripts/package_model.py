import os
import zipfile
import datetime
import sys


def package_model():
    """Package trained model and metadata"""
    print("Starting model packaging...")

    # Check if model exists
    model_path = "models/trained_model.pkl"
    if not os.path.exists(model_path):
        print("[ERROR] Trained model not found. Run training first.")
        return 1

    # Create package directory
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    package_name = f"iris_model_v{timestamp}"
    package_dir = f"packages/{package_name}"

    os.makedirs(package_dir, exist_ok=True)
    os.makedirs("packages", exist_ok=True)

    # Create model info file
    model_info = f"""Model Information
================
Model Type: Random Forest Classifier
Training Date: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Features: 4
Classes: 3
Package Version: {timestamp}
"""

    with open(f"{package_dir}/model_info.txt", "w") as f:
        f.write(model_info)

    # Create deployment script
    deploy_script = """# Model Deployment Script
import joblib
import numpy as np

# Load model
model = joblib.load('trained_model.pkl')

# Example prediction
sample_data = np.array([[1.2, 3.4, 2.1, 0.8]])
prediction = model.predict(sample_data)
print(f"Prediction: {prediction[0]}")
"""

    with open(f"{package_dir}/deploy.py", "w") as f:
        f.write(deploy_script)

    # Copy model file
    import shutil
    shutil.copy(model_path, f"{package_dir}/trained_model.pkl")

    # Create zip package
    zip_path = f"packages/{package_name}.zip"
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for root, dirs, files in os.walk(package_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, package_dir)
                zipf.write(file_path, arcname)

    print(f"[SUCCESS] Model packaged successfully: {zip_path}")
    print(f"Package size: {os.path.getsize(zip_path)} bytes")

    return 0


if __name__ == "__main__":
    exit_code = package_model()
    sys.exit(exit_code)