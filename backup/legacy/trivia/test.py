# -*- coding: utf-8 -*-
import os
from volcenginesdkarkruntime import AsyncArk
import time
import base64
import glob
import asyncio
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=True)

async def main():
    client = AsyncArk( 
        base_url="https://ark.cn-beijing.volces.com/api/v3", 
        api_key=os.getenv('DOUBAO_API_KEY'), 
    ) 

    with open("/Users/renxiaoyao/Documents/eatting.mov", "rb")as file:
        result = await client.files.create(
            file=file,
            purpose="user_data",
        )

    print(result)

    while True:
        get_result = await client.files.retrieve(file_id=result.id)
        status = get_result.status
        if status == "active":
            print("----- task succeeded -----")
            print(get_result)
            print(get_result.download_url)
            break
        elif status == "failed":
            print("----- task failed -----")
            print(f"Error: {get_result.error}")
            break
        else:
            print(f"Current status: {status}, Retrying after 5 seconds...")
            time.sleep(5)

if __name__=="__main__":
    asyncio.run(main())


# def encode_local_image(image_path: str) -> str:

#     image_path = os.path.expanduser(image_path)

#     if not os.path.isfile(image_path):
#         raise FileNotFoundError(
#                 f"本地图片文件不存在或不是有效文件: {image_path}"
#         )
        
#     with open(image_path, "rb") as file:
#         image =  file.read()

#     image_data = base64.b64encode(image).decode("utf-8")

#     return image_data


# async def send_to_feishu(self, local_file: str, open_id: str):

#         with open(local_file, "rb") as file:
            
#             request: CreateImageRequest = CreateImageRequest.builder() \
#                 .request_body(CreateImageRequestBody.builder()
#                     .image_type("message")
#                     .image(file)
#                     .build()) \
#                 .build()

#             # 发起请求
#             response: CreateImageResponse = await asyncio.to_thread(self.feishu_client.im.v1.image.create, request)

#             # 处理失败返回
#             if not response.success():
#                 lark.logger.error(
#                     f"client.im.v1.image.create failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}, resp: \n{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}")
#                 return

#             # 处理业务结果
#             lark.logger.info(lark.JSON.marshal(response.data, indent=4))

#             request: CreateMessageRequest = CreateMessageRequest.builder() \
#                 .receive_id_type("open_id") \
#                 .request_body(CreateMessageRequestBody.builder()
#                     .receive_id(open_id)
#                     .msg_type("image")
#                     .content(f"{{\"image_key\":\"{response.data.image_key}\"}}")
#                     .build()) \
#                 .build()

#             # 发起请求
#             response: CreateMessageResponse = await asyncio.to_thread(self.feishu_client.im.v1.message.create, request)

#             # 处理失败返回
#             if not response.success():
#                 lark.logger.error(
#                     f"client.im.v1.message.create failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}, resp: \n{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}")
#                 return

#             # 处理业务结果
#             lark.logger.info(lark.JSON.marshal(response.data, indent=4))



# async def main():
#     image_dir = "/Users/renxiaoyao/Downloads/image"
#     image_files = glob.glob(os.path.join(image_dir, "*.png"))


    # client = OpenAI( 
    #     base_url="https://ark.cn-beijing.volces.com/api/v3", 
    #     api_key=os.getenv('ARK_API_KEY'), 
    # ) 

#     imagesResponse = client.images.generate( 
#         model="doubao-seedream-5-0-pro-260628",
#         prompt="根据手绘草图对图像进行编辑。在左下角标记区域添加一叠真实的杂志或艺术画册，并在右侧标记区域添加一个带杯碟的陶瓷杯咖啡。移除所有草图线条。保持构图不变。让新添加的物体自然融入原有场景中。",
#         size="2K",
#         output_format="png",
#         response_format="url",
#         extra_body = {
#             "image": [encode_local_image(image_path=image) for image in image_files],
#             "watermark": False
#         }
#     ) 

#     print(imagesResponse.data[0].url)