import os
import subprocess

def print_hello():
    print('Hello World!')

def output_process():
    result = subprocess.run(['ls', '-l'], capture_output=True)
    stdout = result.stdout.decode('utf-8')
    return stdout

def main():
    # print_hello()
    output = output_process()
    print(output)


if __name__ == '__main__':
    main()