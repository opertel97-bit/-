document.getElementById("form-link")?.addEventListener("click", function (e) {
  if (this.getAttribute("href") === "#") {
    e.preventDefault();
    alert("구매 신청 폼 링크를 아직 연결하지 않았습니다. 구글폼 링크를 만든 뒤 이 버튼에 연결해주세요.");
  }
});
