import os
import sys
from grpc_tools import protoc

def compile():
    # proto 디렉토리와 파일 정의
    proto_dir = "proto"
    proto_file = os.path.join(proto_dir, "babyray.proto")
    
    print(f"Compiling {proto_file}...")
    
    # grpc_tools.protoc.main을 사용하여 컴파일
    args = [
        "grpc_tools.protoc",
        f"-I{proto_dir}",
        f"--python_out={proto_dir}",
        f"--grpc_python_out={proto_dir}",
        proto_file
    ]
    
    if protoc.main(args) != 0:
        print("Error: Protobuf compilation failed.")
        sys.exit(1)
        
    print("Compilation successful.")
    
    # 컴파일된 grpc pb2 파일 패치
    grpc_file = os.path.join(proto_dir, "babyray_pb2_grpc.py")
    if os.path.exists(grpc_file):
        print(f"Patching {grpc_file}...")
        with open(grpc_file, "r", encoding="utf-8") as f:
            content = f.read()
            
        patched = False
        
        # 패턴 1: 일반적인 grpcio-tools가 생성하는 임포트 패턴
        old_import_1 = "import babyray_pb2 as babyray__pb2"
        new_import_1 = "from proto import babyray_pb2 as babyray__pb2"
        if old_import_1 in content:
            content = content.replace(old_import_1, new_import_1)
            patched = True
            
        # 패턴 2: 일부 버전이나 설정에 의해 생성될 수 있는 임포트 패턴
        old_import_2 = "import babyray_pb2 as proto_dot_babyray__pb2"
        new_import_2 = "from proto import babyray_pb2 as proto_dot_babyray__pb2"
        if old_import_2 in content:
            content = content.replace(old_import_2, new_import_2)
            patched = True
            
        if patched:
            with open(grpc_file, "w", encoding="utf-8") as f:
                f.write(content)
            print("Patch applied successfully.")
        else:
            print("Warning: Import patterns not found or already patched.")

if __name__ == "__main__":
    compile()
